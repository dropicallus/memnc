"""Orchestrator: durable state machine over NATS JetStream."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from . import acl
from .bus import Bus
from .config import Settings, get_settings
from .fsutil import apply_artifacts
from .fsm import Event, transition
from .schema import (
    CoderOutput,
    CriticOutput,
    Envelope,
    PlannerOutput,
    ResultPayload,
    Role,
    SUBJECT_CMD,
    SUBJECT_HEARTBEAT,
    SUBJECT_SUBMIT,
    TaskSnapshot,
    TaskState,
    TERMINAL_STATES,
    TaskSubmission,
    GrantLevel,
    new_id,
    now_iso,
)

log = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, settings: Settings | None = None, bus: Bus | None = None):
        self.settings = settings or get_settings()
        self.bus = bus or Bus(self.settings)
        self._stop = asyncio.Event()
        self._results = None
        self._submit_sub = None
        self._heartbeat_sub = None

    async def start(self) -> None:
        await self.bus.connect()
        Path(self.settings.workspace_root).mkdir(parents=True, exist_ok=True)
        assert self.bus.nc is not None
        self._submit_sub = await self.bus.nc.subscribe(
            SUBJECT_SUBMIT, cb=self._on_submit
        )
        self._heartbeat_sub = await self.bus.nc.subscribe(
            SUBJECT_HEARTBEAT.format(node="*"), cb=self._on_heartbeat
        )
        self._results = await self.bus.pull_results()
        await self._recover()
        log.info("orchestrator ready", extra={"node": self.settings.node_name})
        await asyncio.gather(self._result_loop(), self._timeout_loop())

    async def stop(self) -> None:
        self._stop.set()

    async def aclose(self) -> None:
        self._stop.set()
        await self.bus.close()

    # -- submission --------------------------------------------------------
    async def _on_submit(self, message) -> None:
        try:
            submission = TaskSubmission.model_validate_json(message.data)
            snapshot = TaskSnapshot(
                task_id=new_id(),
                trace_id=new_id(),
                task=submission.task,
                context=submission.context,
                max_revisions=submission.max_revisions
                if submission.max_revisions is not None
                else self.settings.max_revisions,
                state=TaskState.RECEIVED,
            )
            await self.bus.put_snapshot(snapshot)
            await self.bus.emit_event(snapshot, "task.received", submission.task[:200])
            await message.respond(
                json.dumps({"task_id": snapshot.task_id}).encode()
            )
            log.info(
                "task submitted",
                extra={"task_id": snapshot.task_id, "task": submission.task[:200]},
            )
            await self._apply(snapshot, Event("start"))
        except Exception as exc:  # noqa: BLE001
            log.exception("submission failed")
            try:
                await message.respond(json.dumps({"error": str(exc)}).encode())
            except Exception:  # noqa: BLE001
                pass

    # -- M9 grant bootstrap --------------------------------------------------
    async def _on_heartbeat(self, message) -> None:
        """Issue CHAIN grants for agents of allowed joining nodes.

        Bootstrap for multi-machine meshes: a node listed in
        ``MESH_GRANT_NODES`` gets its agents granted on first heartbeat, so a
        new machine can join without a manual ``meshctl grant``. Revoked
        agents stay revoked — an existing grant record is never overwritten.
        """
        try:
            payload = Envelope.model_validate_json(message.data).payload or {}
            patterns = [
                p for p in self.settings.grant_nodes.split(",") if p.strip()
            ]
            for agent_id in acl.auto_grant_agents(payload, patterns):
                if await self.bus.grant_read(agent_id) is not None:
                    continue
                await self.bus.issue_grant(
                    agent_id, level=GrantLevel.CHAIN,
                    granted_by=f"bootstrap:{self.settings.node_name}",
                )
                log.info("auto-granted %s (heartbeat bootstrap)", agent_id)
        except Exception:  # noqa: BLE001 - bootstrap is best effort
            log.debug("heartbeat bootstrap failed", exc_info=True)

    # -- results -----------------------------------------------------------
    async def _result_loop(self) -> None:
        while not self._stop.is_set():
            messages = await self.bus.fetch(self._results, batch=1, timeout_s=5)
            for message in messages:
                try:
                    await self._handle_result(message)
                except Exception:  # noqa: BLE001 - keep the loop alive
                    log.exception("result handling failed")
                    try:
                        await self.bus.dead_letter(
                            Envelope.model_validate_json(message.data),
                            "orchestrator error",
                        )
                    except Exception:  # noqa: BLE001
                        pass
                finally:
                    try:
                        await message.ack()
                    except Exception:  # noqa: BLE001
                        pass

    async def _handle_result(self, message) -> None:
        envelope = Envelope.model_validate_json(message.data)
        snapshot = await self.bus.get_snapshot(envelope.task_id)
        if snapshot is None:
            await self.bus.dead_letter(envelope, "unknown task")
            return
        if snapshot.state in TERMINAL_STATES:
            log.info("ignoring result for terminal task", extra={"task_id": snapshot.task_id})
            return
        if envelope.id in snapshot.seen_result_ids:
            return

        last_action = snapshot.last_action or {}
        expected_role = last_action.get("role")
        if expected_role and envelope.role and envelope.role.value != expected_role:
            log.warning(
                "ignoring stale result",
                extra={
                    "task_id": snapshot.task_id,
                    "expected": expected_role,
                    "got": envelope.role.value,
                },
            )
            return
        if (
            envelope.correlation_id
            and last_action.get("command_id")
            and envelope.correlation_id != last_action["command_id"]
        ):
            log.info(
                "ignoring result for superseded command",
                extra={"task_id": snapshot.task_id},
            )
            return

        try:
            payload = ResultPayload.model_validate(envelope.payload)
        except Exception as exc:  # noqa: BLE001
            log.warning("bad result payload: %s", exc)
            return

        snapshot.seen_result_ids.append(envelope.id)
        snapshot.seen_result_ids = snapshot.seen_result_ids[-50:]

        event = Event("result", ok=payload.ok, reason=payload.error or "")
        if payload.ok and envelope.role is not None:
            try:
                self._store_output(snapshot, envelope.role, payload.output)
            except Exception as exc:  # noqa: BLE001
                event = Event("result", ok=False, reason=f"invalid output: {exc}")
            if envelope.role is Role.CRITIC and snapshot.critique is not None:
                event = Event(
                    "result",
                    ok=True,
                    approved=snapshot.critique.approved,
                )

        log.info(
            "result received",
            extra={
                "task_id": snapshot.task_id,
                "role": envelope.role.value if envelope.role else None,
                "ok": event.ok,
                "approved": event.approved,
                "executed_on": envelope.node,
            },
        )
        await self._apply(snapshot, event)
        if envelope.node:
            # visibility for meshctl tail: which node executed this stage
            await self.bus.emit_event(
                snapshot, "result.executed_on", envelope.node
            )

    def _store_output(
        self, snapshot: TaskSnapshot, role: Role, output: dict
    ) -> None:
        if role is Role.PLANNER:
            snapshot.plan = PlannerOutput.model_validate(output)
        elif role is Role.CODER:
            snapshot.code = CoderOutput.model_validate(output)
            if self.settings.apply_artifacts:
                written = apply_artifacts(
                    Path(self.settings.workspace_root),
                    snapshot.code.files,
                    self.settings.max_artifact_bytes,
                )
                log.info(
                    "artifacts applied",
                    extra={"task_id": snapshot.task_id, "files": written},
                )
        elif role is Role.CRITIC:
            snapshot.critique = CriticOutput.model_validate(output)

    # -- transitions -------------------------------------------------------
    async def _apply(self, snapshot: TaskSnapshot, event: Event) -> None:
        result = transition(
            snapshot.state,
            event,
            attempt=snapshot.attempt,
            revisions=snapshot.revisions,
            max_attempts=self.settings.max_attempts,
            max_revisions=snapshot.max_revisions,
        )

        changed = (
            result.state is not snapshot.state
            or bool(result.actions)
            or result.attempt_delta
            or result.attempt_reset
            or result.revision_delta
        )
        if not changed:
            return

        snapshot.state = result.state
        snapshot.attempt = 0 if result.attempt_reset else snapshot.attempt + result.attempt_delta
        snapshot.revisions += result.revision_delta
        snapshot.record(event.type, event.reason)
        if event.reason and result.state is TaskState.FAILED:
            snapshot.error = event.reason

        await self.bus.put_snapshot(snapshot)
        await self.bus.emit_event(
            snapshot,
            f"state.{result.state.value.lower()}",
            event.reason,
        )
        log.info(
            "transition",
            extra={
                "task_id": snapshot.task_id,
                "state": result.state.value,
                "event": event.type,
                "revisions": snapshot.revisions,
                "attempt": snapshot.attempt,
            },
        )

        for action in result.actions:
            if action.type == "dispatch" and action.role is not None:
                await self._dispatch(snapshot, action.role)
            elif action.type == "finish":
                snapshot.record("task.done")
                await self.bus.put_snapshot(snapshot)
                await self.bus.emit_event(snapshot, "task.done", "workflow complete")
            elif action.type == "escalate":
                snapshot.record("task.failed", action.reason)
                await self.bus.put_snapshot(snapshot)
                await self.bus.emit_event(snapshot, "task.failed", action.reason)
                await self.bus.dead_letter(
                    Envelope(
                        task_id=snapshot.task_id,
                        trace_id=snapshot.trace_id,
                        kind="event",
                        node=self.settings.node_name,
                        payload={"reason": action.reason, "task": snapshot.task},
                    ),
                    action.reason,
                )
            elif action.type == "cancel":
                snapshot.record("task.cancelled", action.reason)
                await self.bus.put_snapshot(snapshot)
                await self.bus.emit_event(snapshot, "task.cancelled", action.reason)

        if result.state is TaskState.REVISE:
            await self._apply(snapshot, Event("start"))

    async def _dispatch(self, snapshot: TaskSnapshot, role: Role) -> None:
        timeout_s = self.settings.timeout_for(role)
        command = Envelope(
            task_id=snapshot.task_id,
            trace_id=snapshot.trace_id,
            kind="command",
            role=role,
            node=self.settings.node_name,
            state=snapshot.state,
            attempt=snapshot.attempt,
            timeout_ms=timeout_s * 1000,
            payload={
                "task": snapshot.task,
                "context": snapshot.context,
                "plan": snapshot.plan.model_dump() if snapshot.plan else None,
                "code": snapshot.code.model_dump() if snapshot.code else None,
                "critique": snapshot.critique.model_dump()
                if snapshot.critique
                else None,
                "revisions": snapshot.revisions,
                "workspace_root": str(self.settings.workspace_root),
                "role": role.value,
            },
        )
        snapshot.last_action = {
            "command_id": command.id,
            "role": role.value,
            "at": now_iso(),
            "at_epoch": time.time(),
            "timeout_s": timeout_s,
        }
        await self.bus.put_snapshot(snapshot)
        await self.bus.publish_envelope(
            SUBJECT_CMD.format(role=role.value), command
        )
        await self.bus.emit_event(
            snapshot, "dispatch", f"{role.value} command {command.id}"
        )

    # -- timers / recovery -------------------------------------------------
    async def _timeout_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(5)
            try:
                await self._reap_timeouts()
            except Exception:  # noqa: BLE001
                log.exception("timeout reaper failed")

    async def _reap_timeouts(self) -> None:
        snapshots = await self.bus.list_tasks()
        now = time.time()
        for snapshot in snapshots:
            if snapshot.state in TERMINAL_STATES:
                continue
            last = snapshot.last_action
            if not last or "at_epoch" not in last:
                continue
            limit = float(last.get("timeout_s", 60)) + self.settings.orchestrator_grace_s
            if now - float(last["at_epoch"]) > limit:
                log.warning(
                    "step timed out",
                    extra={
                        "task_id": snapshot.task_id,
                        "role": last.get("role"),
                        "state": snapshot.state.value,
                    },
                )
                await self._apply(
                    snapshot,
                    Event(
                        "timeout",
                        ok=False,
                        reason=f"{last.get('role')} timed out after {int(limit)}s",
                    ),
                )

    async def _recover(self) -> None:
        for snapshot in await self.bus.list_tasks():
            if snapshot.state in TERMINAL_STATES:
                continue
            if snapshot.state is TaskState.REVISE:
                await self._apply(snapshot, Event("start"))
            elif snapshot.state is TaskState.RECEIVED and not snapshot.last_action:
                await self._apply(snapshot, Event("start"))
            else:
                log.info(
                    "recovered in-flight task",
                    extra={"task_id": snapshot.task_id, "state": snapshot.state.value},
                )


async def _main() -> None:
    import signal

    from .log import setup_logging

    settings = get_settings()
    setup_logging(settings.log_level)
    orchestrator = Orchestrator(settings)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(
                sig, lambda: asyncio.create_task(orchestrator.stop())
            )
        except NotImplementedError:  # pragma: no cover
            pass
    try:
        await orchestrator.start()
    finally:
        await orchestrator.aclose()


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
