"""Worker runtime: pull a command, run the agent loop, publish a result."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path

from pydantic import ValidationError

from .agent import parse_role_output, run_agent
from .backends import make_backend
from .bus import Bus
from .config import Settings, get_settings
from .fsm import Event, transition
from .fsutil import apply_artifacts
from .mcp.client import MCPClient
from .roles import PROMPT_VERSION, SYSTEM_PROMPTS, build_user_prompt
from .schema import (
    Envelope,
    ResultPayload,
    Role,
    SUBJECT_CMD,
    SUBJECT_HEARTBEAT,
    SUBJECT_RESULT,
    SUBJECT_SUBMIT,
    SUBJECT_SUBMIT_NODE,
    TaskSnapshot,
    TaskSubmission,
    TaskState,
    TERMINAL_STATES,
    ROLE_OUTPUT as _OUTPUT_SCHEMA,
)

log = logging.getLogger(__name__)

MAX_TOOL_RESULT_CHARS = 8_000
MAX_CHECKPOINTS = 32
HEARTBEAT_INTERVAL_S = 5

# M11: mesh_submit — the agent-facing tool that throws a task back into the
# mesh. This is the core of self-orchestration: a coder inside task X can
# submit an independent sub-task itself instead of the orchestrator doing it.
MESH_SUBMIT_TOOL = {
    "type": "function",
    "function": {
        "name": "mesh_submit",
        "description": (
            "Submit an independent task into the mesh. The mesh runs it on its "
            "own pipeline (planner->coder->critic) and returns the new task_id. "
            "Use for sub-goals that are independent of your current deliverable. "
            "Only one level of re-submission is allowed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Full task description for the mesh.",
                },
                "context": {
                    "type": "string",
                    "description": "Optional context for the submitted task.",
                },
                "fanout": {
                    "type": "boolean",
                    "description": "Let the orchestrator split the plan into independent subtasks.",
                },
            },
            "required": ["task"],
        },
    },
}


def tool_key(command_id: str, name: str, arguments: dict) -> str:
    blob = json.dumps(arguments, sort_keys=True, default=str)
    digest = hashlib.sha256(f"{command_id}|{name}|{blob}".encode()).hexdigest()
    return digest[:32]


class Worker:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        backend=None,
        tools=None,
        bus: Bus | None = None,
    ):
        self.settings = settings or get_settings()
        self.bus = bus or Bus(self.settings)
        self.backend = backend
        self.tools = tools
        self._stop = asyncio.Event()
        self._busy = False
        self._current_task: str | None = None
        self._subscription = None

    @property
    def role(self) -> Role:
        return self.settings.role

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        await self.bus.connect()
        Path(self.settings.workspace_root).mkdir(parents=True, exist_ok=True)
        if self.backend is None:
            self.backend = make_backend(self.settings)
        if self.tools is None and self.settings.mcp_enabled:
            client = MCPClient(self.settings)
            await client.start()
            self.tools = client
        self._subscription = await self.bus.pull_commands(self.role)
        log.info(
            "worker ready",
            extra={
                "role": self.role.value,
                "node": self.settings.node_name,
                "model": self.settings.model_for(self.role),
                "mcp": bool(self.tools and getattr(self.tools, "available", False)),
            },
        )
        await asyncio.gather(
            self._command_loop(),
            self._heartbeat_loop(),
            *(
                (self._claim_loop(),) if self.settings.mesh_claim else ()
            ),
        )

    async def stop(self) -> None:
        self._stop.set()

    async def aclose(self) -> None:
        self._stop.set()
        if self.tools is not None:
            try:
                await self.tools.close()
            except Exception:  # noqa: BLE001
                log.debug("tool shutdown failed", exc_info=True)
        if self.backend is not None:
            try:
                await self.backend.close()
            except Exception:  # noqa: BLE001
                log.debug("backend shutdown failed", exc_info=True)
        await self.bus.close()

    # -- loops -------------------------------------------------------------
    async def _command_loop(self) -> None:
        while not self._stop.is_set():
            messages = await self.bus.fetch(self._subscription, batch=1, timeout_s=5)
            for message in messages:
                try:
                    await self._handle(message)
                except Exception:  # noqa: BLE001 - never kill the loop
                    log.exception("command handling failed")
                    try:
                        await message.nak()
                    except Exception:  # noqa: BLE001
                        pass

    async def _heartbeat_loop(self) -> None:
        model = self.settings.model_for(self.role)
        while not self._stop.is_set():
            envelope = Envelope(
                task_id="heartbeat",
                trace_id="heartbeat",
                kind="heartbeat",
                role=self.role,
                node=self.settings.node_name,
                payload={
                    "role": self.role.value,
                    "node": self.settings.node_name,
                    "model": model,
                    "busy": self._busy,
                    "task_id": self._current_task,
                },
            )
            try:
                assert self.bus.nc is not None
                await self.bus.nc.publish(
                    SUBJECT_HEARTBEAT.format(node=self.settings.node_name),
                    envelope.model_dump_json().encode(),
                )
            except Exception:  # noqa: BLE001 - heartbeat is best effort
                log.debug("heartbeat failed", exc_info=True)
            await asyncio.sleep(HEARTBEAT_INTERVAL_S)

    # -- command handling --------------------------------------------------
    async def _handle(self, message) -> None:
        envelope = Envelope.model_validate_json(message.data)
        snapshot = await self.bus.get_snapshot(envelope.task_id)
        if snapshot is None:
            await self.bus.dead_letter(envelope, "unknown task")
            await message.ack()
            return

        self._busy = True
        self._current_task = envelope.task_id
        try:
            timeout_s = (
                envelope.timeout_ms / 1000
                if envelope.timeout_ms
                else float(self.settings.timeout_for(self.role))
            )
            log.info(
                "command received",
                extra={
                    "task_id": envelope.task_id,
                    "role": self.role.value,
                    "command_id": envelope.id,
                    "timeout_s": timeout_s,
                },
            )
            try:
                result = await asyncio.wait_for(
                    self._run(envelope, snapshot), timeout=timeout_s + 5
                )
            except asyncio.TimeoutError:
                result = ResultPayload(ok=False, error="worker timed out")
            except Exception as exc:  # noqa: BLE001
                log.exception("agent run failed", extra={"task_id": envelope.task_id})
                result = ResultPayload(
                    ok=False, error=f"{type(exc).__name__}: {exc}"
                )

            result_envelope = Envelope(
                task_id=envelope.task_id,
                trace_id=envelope.trace_id,
                correlation_id=envelope.id,
                kind="result",
                role=self.role,
                node=self.settings.node_name,
                state=snapshot.state,
                attempt=snapshot.attempt,
                payload=result.model_dump(),
            )
            await self.bus.publish_envelope(
                SUBJECT_RESULT.format(role=self.role.value), result_envelope
            )
            await message.ack()
            log.info(
                "result published",
                extra={
                    "task_id": envelope.task_id,
                    "role": self.role.value,
                    "ok": result.ok,
                    "tool_steps": result.tool_steps,
                },
            )
        finally:
            self._busy = False
            self._current_task = None

    async def _run(self, envelope: Envelope, snapshot: TaskSnapshot) -> ResultPayload:
        role = envelope.role or self.role
        model = self.settings.model_for(role)
        tools = (
            list(self.tools.schemas)
            if self.tools is not None and getattr(self.tools, "available", False)
            else []
        )
        # M11: agents as submitters — every role may throw sub-tasks back
        # into the mesh (bounded by max_submit_depth in the orchestrator).
        tools.append(MESH_SUBMIT_TOOL)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPTS[role]},
            {
                "role": "user",
                "content": build_user_prompt(role, envelope.payload or {}),
            },
        ]

        async def execute(name: str, arguments: dict) -> str:
            return await self._execute_tool(envelope, name, arguments)

        try:
            run = await run_agent(
                backend=self.backend,
                model=model,
                messages=messages,
                tools=tools,
                max_steps=self.settings.max_tool_steps,
                execute_tool=execute,
                temperature=0.2,
                num_ctx=self.settings.num_ctx,
                num_predict=self.settings.num_predict,
            )
        except Exception as exc:  # noqa: BLE001
            return ResultPayload(ok=False, error=f"agent error: {exc}")

        try:
            output = parse_role_output(role, run.content)
        except (ValueError, ValidationError) as exc:
            output = await self._repair(role, model, run, str(exc))
            if output is None:
                return ResultPayload(
                    ok=False, error=f"invalid {role.value} output: {exc}"
                )

        if role is Role.CODER and self.settings.apply_artifacts:
            try:
                apply_artifacts(
                    Path(self.settings.workspace_root),
                    output.files,
                    self.settings.max_artifact_bytes,
                )
            except Exception as exc:  # noqa: BLE001
                return ResultPayload(ok=False, error=f"artifact apply failed: {exc}")

        return ResultPayload(
            ok=True,
            output=output.model_dump(),
            prompt_version=PROMPT_VERSION,
            tool_steps=run.tool_steps,
        )

    async def _repair(self, role: Role, model: str, run, error: str):
        correction = (
            "Your previous response was not valid JSON for the required schema "
            f"({error}). Return ONLY the JSON object, with no prose or fences."
        )
        messages = list(run.messages) + [
            {"role": "assistant", "content": run.content},
            {"role": "user", "content": correction},
        ]
        try:
            repaired = await self.backend.chat(messages, tools=None, model=model)
            return parse_role_output(role, repaired.content)
        except Exception:  # noqa: BLE001
            log.warning("output repair failed", exc_info=True)
            return None

    async def _execute_tool(
        self, envelope: Envelope, name: str, arguments: dict
    ) -> str:
        # M11: mesh_submit is a built-in (no MCP needed) — intercept first.
        if name == "mesh_submit":
            return await self._mesh_submit(envelope, arguments)

        if self.tools is None or not getattr(self.tools, "available", False):
            raise RuntimeError("no MCP tools available")

        key = tool_key(envelope.id, name, arguments)
        snapshot = await self.bus.get_snapshot(envelope.task_id)
        if snapshot is not None and key in snapshot.tool_results:
            log.info(
                "tool replay skipped",
                extra={"task_id": envelope.task_id, "tool": name},
            )
            return snapshot.tool_results[key]

        output = await self.tools.call(name, arguments)
        output = output[:MAX_TOOL_RESULT_CHARS]

        snapshot = await self.bus.get_snapshot(envelope.task_id) or snapshot
        if snapshot is not None:
            snapshot.completed_tool_calls.append(key)
            snapshot.tool_results[key] = output
            while len(snapshot.completed_tool_calls) > MAX_CHECKPOINTS:
                snapshot.completed_tool_calls.pop(0)
            while len(snapshot.tool_results) > MAX_CHECKPOINTS:
                snapshot.tool_results.pop(next(iter(snapshot.tool_results)))
            await self.bus.put_snapshot(snapshot)
        return output

    # -- M11: agents as submitters ------------------------------------------
    async def _mesh_submit(self, envelope: Envelope, arguments: dict) -> str:
        """Throw an independent task back into the mesh (one nesting level)."""
        assert self.bus.nc is not None
        snapshot = await self.bus.get_snapshot(envelope.task_id)
        depth = (snapshot.depth + 1) if snapshot is not None else 1
        if depth >= self.settings.max_submit_depth:
            return json.dumps(
                {
                    "ok": False,
                    "error": f"max_submit_depth ({self.settings.max_submit_depth}) "
                    "reached: no further re-submission from inside this task",
                }
            )
        submission = TaskSubmission(
            task=str(arguments.get("task") or "").strip(),
            context=str(arguments.get("context") or "")[:2_000],
            fanout=bool(arguments.get("fanout")),
            depth=depth,
            claimable=self.settings.mesh_claim,
        )
        if not submission.task:
            return json.dumps({"ok": False, "error": "task is required"})
        response = await self.bus.nc.request(
            # M15: submit to the owning orchestrator (this worker's node).
            SUBJECT_SUBMIT_NODE.format(node=self.settings.node_name),
            submission.model_dump_json().encode(),
            timeout=10,
        )
        data = json.loads(response.data) or {}
        return json.dumps(
            {"ok": True, "task_id": data.get("task_id"), "depth": depth}
        )

    # -- M11: ledger-based self-claiming -------------------------------------
    async def _claim_loop(self) -> None:
        """Poll the ledger and CAS-claim tasks matching our next role.

        M11: claim = CAS update of task snapshot (bus.try_claim); on conflict
        (someone was faster) we simply move on.
        M13: a busy worker must not swallow its own sub-tasks — leaving them
        to the other node is the point of the mesh.
        M14: when the task carries a next_role, only that role may claim it
        and the claiming worker runs the whole pipeline itself (self_pipeline).
        """
        worker_id = f"{self.settings.node_name}:{self.role.value}"
        while not self._stop.is_set():
            await asyncio.sleep(self.settings.claim_poll_s)
            try:
                for snapshot in await self.bus.list_tasks():
                    if snapshot.state in TERMINAL_STATES:
                        continue
                    if not snapshot.claimable or snapshot.claimed_by:
                        continue
                    if self._busy:
                        continue
                    # M14: role gate — only the scheduled next role claims.
                    if snapshot.next_role and snapshot.next_role != self.role.value:
                        continue
                    claimed = await self.bus.try_claim(
                        snapshot.task_id, worker_id, role=self.role.value
                    )
                    if claimed is None:
                        continue
                    log.info(
                        "task claimed",
                        extra={
                            "task_id": claimed.task_id,
                            "worker": worker_id,
                        },
                    )
                    if (
                        self.settings.self_pipeline
                        and self.settings.mesh_claim
                        and claimed.next_role
                    ):
                        await self._self_pipeline(claimed)
            except Exception:  # noqa: BLE001
                log.exception("claim loop failed")

    # -- M14: worker-driven pipeline ---------------------------------------
    async def _self_pipeline(self, snapshot: TaskSnapshot) -> None:
        """Run our own role on a claimed task, then advance the pipeline.

        Reuses the pure FSM (fsm.transition) for state changes, but replaces
        the orchestrator's command dispatch with a ledger handoff: the next
        role is made claimable and any matching worker may claim it.
        """
        self._busy = True
        self._current_task = snapshot.task_id
        try:
            # RECEIVED -> PLANNING without a dispatch action: we ARE the
            # dispatcher now (M14 worker-driven pipeline).
            if snapshot.state is TaskState.RECEIVED:
                start = transition(snapshot.state, Event("start"))
                snapshot.state = start.state
                snapshot.record("start")
                await self.bus.put_snapshot(snapshot)
                await self.bus.emit_event(snapshot, "state.planning", "claimed")
            role = Role(snapshot.next_role or self.role.value)
            envelope = Envelope(
                task_id=snapshot.task_id,
                trace_id=snapshot.trace_id,
                kind="command",
                role=role,
                node=self.settings.node_name,
                state=snapshot.state,
                attempt=snapshot.attempt,
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
            try:
                timeout_s = float(self.settings.timeout_for(role))
                result = await asyncio.wait_for(
                    self._run(envelope, snapshot), timeout=timeout_s + 5
                )
            except asyncio.TimeoutError:
                result = ResultPayload(ok=False, error="worker timed out")
            except Exception as exc:  # noqa: BLE001
                log.exception("self pipeline run failed")
                result = ResultPayload(ok=False, error=f"{type(exc).__name__}: {exc}")

            result_envelope = Envelope(
                task_id=envelope.task_id,
                trace_id=envelope.trace_id,
                correlation_id=envelope.id,
                kind="result",
                role=role,
                node=self.settings.node_name,
                state=snapshot.state,
                attempt=snapshot.attempt,
                payload=result.model_dump(),
            )
            # publish for meshctl tail / a running orchestrator (which will
            # skip it: we already recorded the id in seen_result_ids)
            await self.bus.publish_envelope(
                SUBJECT_RESULT.format(role=role.value), result_envelope
            )
            snapshot = await self.bus.get_snapshot(snapshot.task_id) or snapshot
            await self._advance(snapshot, result_envelope, result)
        finally:
            self._busy = False
            self._current_task = None

    async def _advance(
        self, snapshot: TaskSnapshot, envelope: Envelope, result: ResultPayload
    ) -> None:
        """Worker-side mirror of the orchestrator result handling (M14)."""
        if snapshot.state in {TaskState.DONE, TaskState.FAILED, TaskState.CANCELLED}:
            return
        if envelope.id not in snapshot.seen_result_ids:
            snapshot.seen_result_ids.append(envelope.id)
            snapshot.seen_result_ids = snapshot.seen_result_ids[-50:]

        event = Event("result", ok=result.ok, reason=result.error or "")
        if result.ok and envelope.role is not None:
            try:
                output_model = _OUTPUT_SCHEMA[envelope.role]
                output = output_model.model_validate(result.output)
                self._store_output(snapshot, envelope.role, output)
            except Exception as exc:  # noqa: BLE001
                log.warning("bad output in self pipeline: %s", exc)
                event = Event("result", ok=False, reason=f"invalid output: {exc}")
            if (
                envelope.role is Role.CRITIC
                and snapshot.critique is not None
            ):
                event = Event(
                    "result", ok=True, approved=snapshot.critique.approved
                )
        if envelope.node:
            await self.bus.emit_event(
                snapshot, "result.executed_on", envelope.node
            )
        await self._apply_local(snapshot, event)

    def _store_output(
        self, snapshot: TaskSnapshot, role: Role, output
    ) -> None:
        if role is Role.PLANNER:
            snapshot.plan = output
        elif role is Role.CODER:
            snapshot.code = output
            if self.settings.apply_artifacts:
                apply_artifacts(
                    self.settings.workspace_root,
                    snapshot.code.files,
                    self.settings.max_artifact_bytes,
                )
        elif role is Role.CRITIC:
            snapshot.critique = output

    async def _fanout_local(self, snapshot: TaskSnapshot) -> None:
        """M16: split a multi-step plan into claimable subtasks (worker-side).

        Mirrors the orchestrator's ``_fanout`` but runs inside the claiming
        planner worker. Subtasks go to the parent's origin node; the owner
        orchestrator aggregates via its fanout check.
        """
        assert self.bus.nc is not None
        assert snapshot.plan is not None  # caller guarantees this
        for step in snapshot.plan.plan:
            submission = TaskSubmission(
                task=step.description or f"step {step.id}",
                context=f"fanout parent={snapshot.task_id} step={step.id}: "
                f"{snapshot.task[:120]}",
                depth=snapshot.depth + 1,
                claimable=True,
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
            snapshot.next_role = "coder"
            snapshot.claimable = True
            snapshot.record("task.fanout.empty", "no subtasks; single pipeline")
            await self.bus.put_snapshot(snapshot)
            return
        snapshot.self_assign = True  # parent parks in CODING, waits for subtasks
        snapshot.claimable = False
        snapshot.next_role = ""
        # No at_epoch key: the timeout reaper skips fanout-wait parents; the
        # aggregation runs in the owner orchestrator's fanout check instead.
        snapshot.last_action = {"role": "fanout-wait"}
        snapshot.record("task.fanout", f"{len(snapshot.subtasks)} subtasks")
        await self.bus.put_snapshot(snapshot)
        await self.bus.emit_event(
            snapshot, "state.coding", f"fanout: {len(snapshot.subtasks)} subtasks"
        )

    async def _apply_local(
        self, snapshot: TaskSnapshot, event: Event
    ) -> None:
        result = transition(
            snapshot.state,
            event,
            attempt=snapshot.attempt,
            revisions=snapshot.revisions,
            max_attempts=self.settings.max_attempts,
            max_revisions=snapshot.max_revisions,
        )
        snapshot.state = result.state
        snapshot.attempt = (
            0 if result.attempt_reset else snapshot.attempt + result.attempt_delta
        )
        snapshot.revisions += result.revision_delta
        snapshot.record(event.type, event.reason)
        if event.reason and result.state is TaskState.FAILED:
            snapshot.error = event.reason

        for action in result.actions:
            if action.type == "dispatch" and action.role is not None:
                # M14 handoff: instead of a command, the next role becomes
                # claimable — any matching worker on any node can pick it up.
                snapshot.next_role = action.role.value
                snapshot.claimed_by = ""
                snapshot.claimable = True
            elif action.type == "finish":
                snapshot.next_role = ""
                snapshot.claimable = False
                snapshot.record("task.done")
            elif action.type == "escalate":
                snapshot.next_role = ""
                snapshot.claimable = False
                snapshot.record("task.failed", action.reason)
                snapshot.error = action.reason
            elif action.type == "cancel":
                snapshot.next_role = ""
                snapshot.claimable = False
                snapshot.record("task.cancelled", action.reason)

        # M16 worker-driven fan-out: the planning worker splits the plan into
        # claimable subtasks itself instead of handing the parent back to an
        # orchestrator. Aggregation stays with the owner orchestrator.
        if (
            result.state is TaskState.CODING
            and snapshot.fanout
            and snapshot.plan is not None
            and len(snapshot.plan.plan) > 1
        ):
            await self._fanout_local(snapshot)
            return

        await self.bus.put_snapshot(snapshot)
        await self.bus.emit_event(
            snapshot, f"state.{result.state.value.lower()}", event.reason
        )
        log.info(
            "self pipeline transition",
            extra={
                "task_id": snapshot.task_id,
                "state": result.state.value,
                "next_role": snapshot.next_role,
                "revisions": snapshot.revisions,
            },
        )
        if result.state is TaskState.REVISE:
            await self._apply_local(snapshot, Event("start"))


async def _main() -> None:
    from .log import setup_logging

    settings = get_settings()
    setup_logging(settings.log_level)
    worker = Worker(settings)
    loop = asyncio.get_running_loop()
    for sig in ("SIGINT", "SIGTERM"):
        try:
            loop.add_signal_handler(
                getattr(__import__("signal"), sig),
                lambda: asyncio.create_task(worker.stop()),
            )
        except NotImplementedError:  # pragma: no cover - non-unix
            pass
    try:
        await worker.start()
    finally:
        await worker.aclose()


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
