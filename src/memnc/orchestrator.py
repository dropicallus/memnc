"""Orchestrator: durable state machine over NATS JetStream."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from . import acl
from .acl import check_registry_write
from .bus import Bus
from .config import Settings, get_settings
from .fsutil import apply_artifacts
from .fsm import Event, transition
from .schema import (
    CoderOutput,
    CriticOutput,
    Envelope,
    Kind,
    PlannerOutput,
    ResultPayload,
    plan_model_request,
    Role,
    SUBJECT_CMD,
    SUBJECT_CONTROL,
    SUBJECT_HEARTBEAT,
    SUBJECT_LEDGER,
    SUBJECT_SUBMIT,
    SUBJECT_SUBMIT_NODE,
    TaskSnapshot,
    TaskState,
    TERMINAL_STATES,
    TaskSubmission,
    ControlCommand,
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
            # M15: wildcard — every orchestrator sees all submissions but only
            # handles those addressed to its own node (target = last token).
            SUBJECT_SUBMIT_NODE.format(node="*"),
            cb=self._on_submit,
        )
        # M10: queryable ledger — any granted agent can read the mesh state.
        self._ledger_sub = await self.bus.nc.subscribe(
            SUBJECT_LEDGER, cb=self._on_ledger
        )
        self._heartbeat_sub = await self.bus.nc.subscribe(
            SUBJECT_HEARTBEAT.format(node="*"), cb=self._on_heartbeat
        )
        # T2: human intervention commands (submit/cancel/retry/stop_worker)
        # from approved channels — grant-checked, then ledger-recorded.
        self._control_sub = await self.bus.nc.subscribe(
            SUBJECT_CONTROL, cb=self._on_control
        )
        self._results = await self.bus.pull_results(
            durable=f"orchestrator-results-{self.settings.node_name}"
        )
        await self._recover()
        log.info("orchestrator ready", extra={"node": self.settings.node_name})
        await asyncio.gather(self._result_loop(), self._timeout_loop())

    async def stop(self) -> None:
        self._stop.set()

    async def aclose(self) -> None:
        self._stop.set()
        await self.bus.close()

    # -- submission --------------------------------------------------------
    def _owns(self, snapshot: TaskSnapshot) -> bool:
        """M15: only the origin node's orchestrator acts on a task.

        Snapshots created before M15 have an empty origin_node; those legacy
        tasks are handled only by the default orchestrator node.
        """
        # Snapshots created before M15 have an empty origin_node; those
        # legacy tasks stay visible to every orchestrator (claim-guard in
        # bus.try_claim already prevents double-claiming).
        return snapshot.origin_node in ("", self.settings.node_name)

    async def _on_submit(self, message) -> None:
        try:
            # M15: only the addressed node's orchestrator handles a
            # submission; the others stay silent (no double task creation).
            # Legacy: the default orchestrator (node_name "orchestrator")
            # also accepts "node-a" (the default NODE_NAME in .env).
            target_node = message.subject.split(".")[-1]
            accepted = {self.settings.node_name}
            if self.settings.node_name == "orchestrator":
                accepted.add("node-a")
            if target_node not in accepted:
                return
            submission = TaskSubmission.model_validate_json(message.data)
            snapshot = await self._accept_submission(submission)
            await message.respond(json.dumps({"task_id": snapshot.task_id}).encode())
        except Exception as exc:  # noqa: BLE001
            log.exception("submission failed")
            try:
                await message.respond(json.dumps({"error": str(exc)}).encode())
            except Exception:  # noqa: BLE001
                pass

    async def _accept_submission(
        self, submission: TaskSubmission, source: str = "meshctl"
    ) -> TaskSnapshot:
        """Create the snapshot and start the pipeline (shared by submit paths)."""
        # M11: recursion limit for agents-as-submitters.
        if submission.depth >= self.settings.max_submit_depth:
            raise ValueError(
                f"submission depth {submission.depth} >= "
                f"max_submit_depth {self.settings.max_submit_depth}"
            )
        snapshot = TaskSnapshot(
            task_id=new_id(),
            trace_id=new_id(),
            task=submission.task,
            context=submission.context,
            max_revisions=submission.max_revisions
            if submission.max_revisions is not None
            else self.settings.max_revisions,
            fanout=submission.fanout,
            depth=submission.depth,
            claimable=submission.claimable,
            needs=submission.needs,
            gate=submission.gate,
            # M14: worker-driven pipeline for claimable tasks
            next_role=("planner" if submission.claimable and self.settings.self_pipeline else ""),
            # M15: the orchestrator that accepted the submission owns it.
            origin_node=self.settings.node_name,
            state=TaskState.RECEIVED,
        )
        await self.bus.put_snapshot(snapshot)
        await self.bus.emit_event(snapshot, "task.received", f"[{source}] {submission.task[:180]}")
        log.info(
            "task submitted",
            extra={"task_id": snapshot.task_id, "task": submission.task[:200]},
        )
        if submission.claimable:
            # M11: referee mode — the orchestrator does NOT dispatch.
            # The task waits in RECEIVED until an agent CAS-claims it
            # (bus.try_claim); _timeout_loop then dispatches claimed ones.
            await self.bus.emit_event(snapshot, "task.claimable", "waiting for claim")
        else:
            await self._apply(snapshot, Event("start"))
        await self._maybe_request_model(snapshot)
        return snapshot

    # -- T2: human intervention over approved channels -----------------------

    async def _on_control(self, message) -> None:
        """Grant-checked human commands: submit / cancel / retry / stop_worker.

        Deny-by-default: the command's agent_id must hold a valid CHAIN grant
        (the Telegram observer gets one via ``meshctl grant`` once). Every
        accepted action is recorded on the task snapshot as a human.* event.
        """
        try:
            cmd = ControlCommand.model_validate_json(message.data)
            grant = await self.bus.grant_read(cmd.agent_id)
            decision = check_registry_write(grant, cmd.agent_id)
            if not decision:
                await message.respond(json.dumps({"error": decision.reason}).encode())
                return
            if cmd.action == "submit":
                if not cmd.task.strip():
                    raise ValueError("submit needs a task")
                snapshot = await self._accept_submission(
                    TaskSubmission(
                        task=cmd.task.strip(),
                        gate=cmd.gate_value() if cmd.gate else "never",
                    ),
                    source=f"human:{cmd.agent_id}",
                )
                snapshot.record("human.submit", f"by={cmd.agent_id} via control")
                await self.bus.put_snapshot(snapshot)
                await message.respond(
                    json.dumps({"ok": True, "task_id": snapshot.task_id}).encode()
                )
            elif cmd.action == "cancel":
                snapshot = await self._human_task_op(cmd)
                await self._apply(snapshot, Event("cancel", reason=f"by {cmd.agent_id}"))
                await message.respond(json.dumps({"ok": True}).encode())
            elif cmd.action == "retry":
                snapshot = await self._human_task_op(cmd)
                if snapshot.state not in (TaskState.FAILED, TaskState.CANCELLED):
                    raise ValueError(
                        f"retry needs a FAILED/CANCELLED task, {snapshot.task_id} is {snapshot.state.value}"
                    )
                snapshot.state = TaskState.RECEIVED
                snapshot.record("human.retry", f"by={cmd.agent_id}")
                await self.bus.put_snapshot(snapshot)
                await self.bus.emit_event(
                    snapshot, "human.retry", f"re-dispatched by {cmd.agent_id}"
                )
                await self._apply(snapshot, Event("start"))
                await self._maybe_request_model(snapshot)
                await message.respond(json.dumps({"ok": True}).encode())
            elif cmd.action == "decide":
                # T3 approval gate: approve/reject a parked task.
                snapshot = await self._human_task_op(cmd)
                if not snapshot.needs_human:
                    raise ValueError(
                        f"task {snapshot.task_id} is not parked ({snapshot.state.value})"
                    )
                if cmd.approved is None:
                    raise ValueError("decide needs approved true/false")
                snapshot.needs_human = False
                await self.bus.put_snapshot(snapshot)
                verdict = "approved" if cmd.approved else "rejected"
                await self.bus.emit_event(
                    snapshot, f"human.decision", f"{verdict} by {cmd.agent_id}"
                )
                await self._apply(
                    snapshot, Event("human_decision", approved=cmd.approved)
                )
                await message.respond(
                    json.dumps(
                        {
                            "ok": True,
                            "task_id": snapshot.task_id,
                            "verdict": verdict,
                            "state": snapshot.state.value,
                        }
                    ).encode()
                )
            elif cmd.action == "stop_worker":
                if not cmd.node.strip():
                    raise ValueError("stop_worker needs a node name")
                # Withdraw every capability record on that node so it receives
                # no further work; the process itself keeps running until the
                # operator stops it on the host (plan §14.6 T2 scope note).
                caps = await self.bus.list_capabilities()
                withdrawn = [
                    cap.agent_id for cap in caps if cap.node == cmd.node.strip()
                ]
                for agent_id in sorted(set(withdrawn)):
                    await self.bus.withdraw_capability(agent_id)
                log.info(
                    "worker stopped by human",
                    extra={"node": cmd.node, "by": cmd.agent_id, "agents": withdrawn},
                )
                await message.respond(json.dumps({"ok": True}).encode())
        except Exception as exc:  # noqa: BLE001
            log.exception("control command failed")
            try:
                await message.respond(json.dumps({"error": str(exc)}).encode())
            except Exception:  # noqa: BLE001
                pass

    async def _human_task_op(self, cmd: ControlCommand) -> TaskSnapshot:
        """Fetch the target task and record the human action on it."""
        if not cmd.task_id.strip():
            raise ValueError(f"{cmd.action} needs a task_id")
        snapshot = await self.bus.get_snapshot(cmd.task_id.strip())
        if snapshot is None:
            raise ValueError(f"unknown task {cmd.task_id.strip()}")
        snapshot.record(f"human.{cmd.action}", f"by={cmd.agent_id}")
        await self.bus.put_snapshot(snapshot)
        return snapshot

    # -- T7.5: model demand planning (plan §14.7) ----------------------------
    async def _maybe_request_model(self, snapshot: TaskSnapshot) -> None:
        """Raise a human-gated ModelRequest when needs.model is unmet everywhere.

        Loaded nowhere → "load" if some node has it on disk, else "pull" (T7).
        One pending request per model, ever — no request spam on resubmits.
        Best effort: a failure here must never break submission.
        """
        try:
            request = plan_model_request(
                snapshot.needs, await self.bus.list_nodes(), snapshot.task_id
            )
            if request is None:
                return
            existing = await self.bus.list_model_requests()
            if any(r.model == request.model and r.status == "pending" for r in existing):
                log.info("model request for %s already pending", request.model)
                return
            await self.bus.put_model_request(request)
            detail = (
                f"model {request.model} not loaded anywhere; "
                f"on disk at {', '.join(request.nodes_disk)}"
                if request.action == "load"
                else f"model {request.model} nowhere loaded or on disk; needs pull"
            )
            await self.bus.emit_event(snapshot, "model.request", detail)
            log.info(
                "model request %s raised (%s)", request.request_id, detail,
                extra={"task_id": snapshot.task_id},
            )
        except Exception:  # noqa: BLE001 - model planning is best effort
            log.debug("model request planning failed", exc_info=True)

    # -- M10: queryable ledger ----------------------------------------------
    async def _on_ledger(self, message) -> None:
        """Answer mesh.ledger requests with the consolidated mesh state."""
        try:
            await message.respond(
                json.dumps(await self.bus.read_ledger()).encode()
            )
        except Exception:  # noqa: BLE001 - ledger read is best effort
            log.exception("ledger read failed")
            try:
                await message.respond(json.dumps({"error": "ledger read failed"}).encode())
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
        if not self._owns(snapshot):  # M15: another node's orchestrator owns it
            return
        if snapshot.needs_human:  # T3: parked at the approval gate
            log.info(
                "ignoring result for parked task", extra={"task_id": snapshot.task_id}
            )
            return
        if envelope.id in snapshot.seen_result_ids:
            return
        # M14: worker-driven pipeline — the claiming worker advances the
        # snapshot itself; do not double-handle its results.
        if snapshot.next_role:
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
            # M10 hive fan-out: a multi-step plan on a fanout task is split
            # into independent subtasks; the parent waits and aggregates.
            if (
                event.ok
                and envelope.role is Role.PLANNER
                and snapshot.fanout
                and snapshot.plan is not None
                and len(snapshot.plan.plan) >= 2
            ):
                await self._fanout(snapshot)
                return

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
            gate=snapshot.gate,
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
        if event.type in ("result", "failure", "timeout") and not event.ok:
            snapshot.error = event.reason or snapshot.error
        elif event.type == "result" and event.ok:
            snapshot.error = None
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
            elif action.type == "park":
                # T3 approval gate: park at CRITIQUE for a human decision.
                snapshot.needs_human = True
                snapshot.record("task.parked", action.reason)
                await self.bus.put_snapshot(snapshot)
                verdict = "approved" if event.approved else "rejected"
                summary = (
                    snapshot.critique.summary if snapshot.critique else ""
                )
                await self.bus.emit_event(
                    snapshot,
                    "task.parked",
                    f"critic {verdict}; {action.reason} — /approve or /reject "
                    f"{snapshot.task_id} | {summary}",
                )

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
                "last_error": snapshot.error,
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

    # -- M10: fan-out (hive run_worker shape) -------------------------------
    async def _fanout(self, snapshot: TaskSnapshot) -> None:
        """Emit each plan step as an independent subtask on mesh.task.submit.

        Returns immediately after emission: subtasks run their own full
        pipeline (planner->coder->critic) on whichever node's workers claim
        them; the parent aggregates in ``_check_fanouts``.
        """
        assert self.bus.nc is not None
        assert snapshot.plan is not None  # caller guarantees this
        for step in snapshot.plan.plan:
            submission = TaskSubmission(
                task=step.description or f"step {step.id}",
                context=f"fanout parent={snapshot.task_id} step={step.id}: "
                f"{snapshot.task[:120]}",
                depth=snapshot.depth + 1,
                claimable=snapshot.claimable,
            )
            try:
                response = await self.bus.nc.request(
                    SUBJECT_SUBMIT_NODE.format(
                        node=snapshot.origin_node or self.settings.node_name
                    ),
                    submission.model_dump_json().encode(),
                    timeout=10,
                )
                sub_id = (json.loads(response.data) or {}).get("task_id")
            except Exception:  # noqa: BLE001 - skip this step, keep fanning out
                log.exception("fanout submit failed for step %s", step.id)
                continue
            if not sub_id:
                continue
            snapshot.subtasks[sub_id] = step.id
            sub = await self.bus.get_snapshot(sub_id)
            if sub is not None:
                sub.parent_task_id = snapshot.task_id
                await self.bus.put_snapshot(sub)
        if not snapshot.subtasks:
            # nothing was fanned out — continue the normal single pipeline
            await self._apply(snapshot, Event("result", ok=True))
            return
        snapshot.self_assign = True  # parent parks in CODING, waits for subtasks
        # No at_epoch key: the timeout reaper skips fanout-wait parents; the
        # aggregation runs in _check_fanouts instead.
        snapshot.last_action = {"role": "fanout-wait"}
        snapshot.record("task.fanout", f"{len(snapshot.subtasks)} subtasks")
        await self.bus.put_snapshot(snapshot)
        await self.bus.emit_event(
            snapshot, "task.fanout", f"{len(snapshot.subtasks)} subtasks emitted"
        )
        log.info(
            "fanout",
            extra={"task_id": snapshot.task_id, "subtasks": len(snapshot.subtasks)},
        )

    async def _check_fanouts(self) -> None:
        """Aggregate fan-out parents whose subtasks have all gone terminal."""
        for snapshot in await self.bus.list_tasks():
            if not self._owns(snapshot):
                continue
            if snapshot.state in TERMINAL_STATES or not snapshot.subtasks:
                continue
            states: list[TaskState] = []
            for sub_id in snapshot.subtasks:
                sub = await self.bus.get_snapshot(sub_id)
                states.append(sub.state if sub is not None else TaskState.FAILED)
            if not all(s in TERMINAL_STATES for s in states):
                continue
            if any(s in (TaskState.FAILED, TaskState.CANCELLED) for s in states):
                reason = "fanout: a subtask failed or was cancelled"
                snapshot.state = TaskState.FAILED
                snapshot.error = reason
                snapshot.record("task.failed", reason)
                await self.bus.put_snapshot(snapshot)
                await self.bus.emit_event(snapshot, "task.failed", reason)
                await self.bus.dead_letter(
                    Envelope(
                        task_id=snapshot.task_id,
                        trace_id=snapshot.trace_id,
                        kind=Kind.EVENT,
                        node=self.settings.node_name,
                        payload={"reason": reason, "task": snapshot.task},
                    ),
                    reason,
                )
                log.warning("fanout failed", extra={"task_id": snapshot.task_id})
            else:
                snapshot.state = TaskState.DONE
                snapshot.record("task.done", "fanout complete")
                await self.bus.put_snapshot(snapshot)
                await self.bus.emit_event(snapshot, "task.done", "fanout complete")
                log.info("fanout done", extra={"task_id": snapshot.task_id})

    # -- timers / recovery -------------------------------------------------
    async def _timeout_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(5)
            try:
                await self._reap_timeouts()
                await self._check_fanouts()
                await self._dispatch_claimed()
            except Exception:  # noqa: BLE001
                log.exception("timeout reaper failed")

    async def _reap_timeouts(self) -> None:
        snapshots = await self.bus.list_tasks()
        now = time.time()
        for snapshot in snapshots:
            if not self._owns(snapshot):
                continue
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

    async def _dispatch_claimed(self) -> None:
        """M11 referee: dispatch claimable tasks that have been CAS-claimed.

        Agents claim via bus.try_claim (CAS on the KV entry). The orchestrator
        does not assign — it only validates a claim and starts the pipeline.
        """
        for snapshot in await self.bus.list_tasks():
            if not self._owns(snapshot):
                continue
            if (
                snapshot.state is TaskState.RECEIVED
                and snapshot.claimable
                and snapshot.claimed_by
                and not snapshot.last_action
                and not snapshot.next_role  # M14: worker-driven, not ours
            ):
                log.info(
                    "claim accepted",
                    extra={
                        "task_id": snapshot.task_id,
                        "claimed_by": snapshot.claimed_by,
                    },
                )
                snapshot.record("task.claimed", snapshot.claimed_by)
                await self.bus.put_snapshot(snapshot)
                await self._apply(snapshot, Event("start"))

    async def _recover(self) -> None:
        for snapshot in await self.bus.list_tasks():
            if not self._owns(snapshot):
                continue
            if snapshot.state in TERMINAL_STATES:
                continue
            if snapshot.state is TaskState.REVISE:
                await self._apply(snapshot, Event("start"))
            elif (
                snapshot.state is TaskState.RECEIVED
                and not snapshot.last_action
                and not (snapshot.claimable and not snapshot.claimed_by)
            ):
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
