"""Worker runtime: pull a command, run the agent loop, publish a result."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sys
from pathlib import Path

from pydantic import ValidationError

from . import clarify
from .agent import extract_json, parse_role_output, run_agent
from .backends import make_backend
from .bus import Bus, emit_debate
from .config import Settings, get_settings
from .fsm import Event, transition
from .fsutil import apply_artifacts
from .harnesses import get_harness
from .harnesses.workspace import changed_file_contents
from .mcp.client import MCPClient
from .roles import (
    CONSULT_SYSTEM_PROMPT,
    PROMPT_VERSION,
    SYSTEM_PROMPTS,
    build_consult_user_prompt,
    build_user_prompt,
)
from .schema import (
    ROLE_OUTPUT as _OUTPUT_SCHEMA,
)
from .schema import (
    SUBJECT_HEARTBEAT,
    SUBJECT_RESULT,
    SUBJECT_SUBMIT_NODE,
    TERMINAL_STATES,
    CoderOutput,
    ConsultAnswer,
    ConsultRequest,
    CriticIssue,
    Envelope,
    FileArtifact,
    ModelRequest,
    NodeInfo,
    ResultPayload,
    Role,
    TaskSnapshot,
    TaskState,
    TaskSubmission,
    model_pull_allowed,
    new_id,
    reputation_key,
    should_propose_retirement,
)
from .synergy import compute_server_success, should_withdraw

log = logging.getLogger(__name__)


async def _no_tool(name: str, arguments: dict) -> str:  # pragma: no cover
    """Consults run without tools; never called."""
    raise RuntimeError("consult runs have no tools")


MAX_TOOL_RESULT_CHARS = 8_000
MAX_CHECKPOINTS = 32
DISK_MODELS_REFRESH_LOOPS = 12  # refresh disk-model list every ~60s of heartbeats


async def fetch_disk_models(base_url: str) -> list[str]:
    """T7.5: list models present in the local Ollama store (loadable, not loaded).

    Best effort: any failure returns [] and the roster simply shows fewer
    disk models. Runs the blocking HTTP call in a thread.
    """
    import json as _json
    import urllib.request

    def _fetch() -> list[str]:
        with urllib.request.urlopen(  # noqa: S310 - configured internal URL
            f"{base_url.rstrip('/')}/api/tags", timeout=5
        ) as resp:
            data = _json.loads(resp.read())
        return sorted(m["name"] for m in data.get("models", []))

    try:
        return await asyncio.to_thread(_fetch)
    except Exception:  # noqa: BLE001 - roster advertisement is best effort
        log.debug("disk model fetch failed", exc_info=True)
        return []


async def load_model(base_url: str, model: str, keep_alive: str = "30m") -> bool:
    """T7.5: load a model from the local Ollama store into VRAM (approved request).

    An empty prompt on /api/generate makes Ollama load the model and keep it
    resident for `keep_alive` without generating anything. Best effort: the
    result is reported through a task event, not raised.
    """
    import json as _json
    import urllib.request

    def _load() -> bool:
        body = _json.dumps({"model": model, "keep_alive": keep_alive}).encode()
        req = urllib.request.Request(  # noqa: S310 - configured internal URL
            f"{base_url.rstrip('/')}/api/generate",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=300) as resp:
            return resp.status == 200

    try:
        ok = await asyncio.to_thread(_load)
        log.info("model load %s -> %s", model, "ok" if ok else "failed")
        return ok
    except Exception:  # noqa: BLE001 - load is best effort, reported via event
        log.exception("model load %s failed", model)
        return False


async def pull_model(base_url: str, model: str) -> bool:
    """T7: pull a model from the Ollama registry onto local disk (approved).

    Uses stream=false so the call returns one JSON status object instead of an
    ndjson progress stream. Potentially long (multi-GB) — generous timeout.
    """
    import json as _json
    import urllib.request

    def _pull() -> bool:
        body = _json.dumps({"name": model, "stream": False}).encode()
        req = urllib.request.Request(  # noqa: S310 - configured internal URL
            f"{base_url.rstrip('/')}/api/pull",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=3600) as resp:
            data = _json.loads(resp.read())
        return data.get("status") == "success"

    try:
        ok = await asyncio.to_thread(_pull)
        log.info("model pull %s -> %s", model, "ok" if ok else "failed")
        return ok
    except Exception:  # noqa: BLE001 - pull is best effort, reported via event
        log.exception("model pull %s failed", model)
        return False


async def unload_model(base_url: str, model: str) -> bool:
    """T7: unload a model from VRAM (retirement approved, keep_alive=0).

    The model stays on disk — retirement only frees VRAM and stops the mesh
    from planning around it.
    """
    import json as _json
    import urllib.request

    def _unload() -> bool:
        body = _json.dumps({"model": model, "keep_alive": 0}).encode()
        req = urllib.request.Request(  # noqa: S310 - configured internal URL
            f"{base_url.rstrip('/')}/api/generate",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status == 200

    try:
        ok = await asyncio.to_thread(_unload)
        log.info("model unload %s -> %s", model, "ok" if ok else "failed")
        return ok
    except Exception:  # noqa: BLE001 - unload is best effort
        log.exception("model unload %s failed", model)
        return False


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


def python_syntax_errors(files: dict[str, str]) -> list[str]:
    """Compile Python sources and return human-readable syntax errors."""

    errors: list[str] = []
    for path, content in files.items():
        if not path.endswith(".py"):
            continue
        try:
            compile(content, path, "exec")
        except SyntaxError as exc:
            errors.append(f"{path}:{exc.lineno}: {exc.msg}")
    return errors


def merge_written_files(output: CoderOutput, written: dict[str, str]) -> dict[str, str]:
    """Combine artifacts returned as JSON with files written through tools.

    Small models often use ``write_file`` and then return an empty ``files``
    list. Adopt those writes so the orchestrator and critic see the artifact.
    """

    merged = dict(written)
    merged.update({item.path: item.content for item in output.files})
    return merged


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
        self._written_files: dict[str, str] = {}
        self._heartbeat_loops = 0
        self._disk_models: list[str] = []

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
            self._model_decision_loop(),
            *((self._claim_loop(),) if self.settings.mesh_claim else ()),
        )

    async def stop(self) -> None:
        self._stop.set()

    async def _paused(self) -> bool:
        """T4: True while this node is human-paused (no claims)."""
        node = await self.bus.get_node(self.settings.node_name)
        return node is not None and node.paused

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

    # -- T7.5: model decisions (plan §14.7) ----------------------------------
    async def _model_decision_loop(self) -> None:
        """Watch approve/reject decisions; on approve, load the model from disk.

        Every node sees the decision (broadcast), but only nodes that actually
        hold the model on disk act — the others ack and ignore.
        """
        try:
            subscription = await self.bus.pull_model_decisions(
                durable=f"worker-{self.settings.node_name}-model-decisions"
            )
        except Exception:  # noqa: BLE001 - never kill startup over this
            log.exception("model decision subscription failed")
            return
        while not self._stop.is_set():
            messages = await self.bus.fetch(subscription, batch=1, timeout_s=5)
            for message in messages:
                try:
                    await self._handle_model_decision(message)
                except Exception:  # noqa: BLE001 - never kill the loop
                    log.exception("model decision handling failed")
                    try:
                        await message.nak()
                    except Exception:  # noqa: BLE001
                        pass

    async def _handle_model_decision(self, message) -> None:
        envelope = Envelope.model_validate_json(message.data)
        request = ModelRequest.model_validate(envelope.payload.get("request", {}))
        if request.status != "approved":
            await message.ack()
            return
        base_url = self.settings.inference_base_url
        if request.action == "pull":
            # T7: approved pull — whitelist was checked by the approver's side
            # as a guard, but re-check locally; never pull unlisted models.
            if not model_pull_allowed(request.model, self.settings.model_pull_whitelist):
                log.warning("approved pull %s refused: not whitelisted", request.model)
                await message.ack()
                return
            ok = await pull_model(base_url, request.model)
            if ok:
                self._disk_models = await fetch_disk_models(base_url)
                await load_model(base_url, request.model)
            log.info(
                "model pull executed",
                extra={
                    "node": self.settings.node_name,
                    "model": request.model,
                    "request": request.request_id,
                    "ok": ok,
                },
            )
            await message.ack()
            return
        if request.action == "retire":
            # T7: approved retirement — unload from VRAM; the model stays on
            # disk. Mark the reputation entry so the claim gate skips it.
            ok = await unload_model(base_url, request.model)
            try:
                rep = await self.bus.get_reputation(request.model)
                rep.retired = True
                assert self.bus.kv is not None
                await self.bus.kv.put(
                    reputation_key(request.model),
                    rep.model_dump_json().encode(),
                )
            except Exception:  # noqa: BLE001 - reputation is best effort
                log.debug("retire reputation update failed", exc_info=True)
            log.info(
                "model retirement executed",
                extra={
                    "node": self.settings.node_name,
                    "model": request.model,
                    "request": request.request_id,
                    "ok": ok,
                },
            )
            await message.ack()
            return
        if request.model not in self._disk_models:
            # Not on this node's disk — another node owns the load.
            await message.ack()
            return
        ok = await load_model(base_url, request.model)
        log.info(
            "model decision executed",
            extra={
                "node": self.settings.node_name,
                "model": request.model,
                "request": request.request_id,
                "ok": ok,
            },
        )
        await message.ack()

    async def _heartbeat_loop(self) -> None:
        model = self.settings.model_for(self.role)
        while not self._stop.is_set():
            self._heartbeat_loops += 1
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
            # T6: refresh this node's roster entry (models/VRAM/GPU merged in).
            # T7.5: also advertise models present on local disk (loadable but
            # not loaded), refreshed every DISK_MODELS_REFRESH_LOOPS beats.
            try:
                if self._heartbeat_loops % DISK_MODELS_REFRESH_LOOPS == 0:
                    self._disk_models = await fetch_disk_models(self.settings.inference_base_url)
                await self.bus.advertise_node(
                    NodeInfo(
                        node_id=self.settings.node_name,
                        roles=[self.role.value],
                        models=[model],
                        disk_models=list(self._disk_models),
                        vram_gb=self.settings.node_vram_gb,
                        gpu=self.settings.node_gpu,
                    )
                )
            except Exception:  # noqa: BLE001 - roster refresh is best effort
                log.debug("roster advertise failed", exc_info=True)
            await asyncio.sleep(HEARTBEAT_INTERVAL_S)

    # -- command handling --------------------------------------------------
    async def _handle(self, message) -> None:
        envelope = Envelope.model_validate_json(message.data)
        # Weak->strong routing: a command with prefer_node set is for that
        # node only — everyone else naks so a matching worker claims it.
        prefer = envelope.payload.get("prefer_node", "")
        if prefer and prefer != self.settings.node_name:
            try:
                await message.nak(delay=2.0)
            except Exception:  # noqa: BLE001
                await message.nak()
            return
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
            except TimeoutError:
                result = ResultPayload(ok=False, error="worker timed out")
            except Exception as exc:  # noqa: BLE001
                log.exception("agent run failed", extra={"task_id": envelope.task_id})
                result = ResultPayload(ok=False, error=f"{type(exc).__name__}: {exc}")

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
        """Run one role step; stamp the executed model onto the result (T4)."""
        result = await self._run_impl(envelope, snapshot)
        result.model = self.settings.model_for(envelope.role or self.role)
        return result

    async def _run_consult(
        self, envelope: Envelope, role: Role, model: str, consult: dict
    ) -> ResultPayload:
        """M16 mesh-clarify: answer a peer's question as a consult."""
        try:
            request = ConsultRequest.model_validate(consult)
        except ValidationError as exc:
            return ResultPayload(ok=False, error=f"invalid consult request: {exc}")
        messages = [
            {"role": "system", "content": CONSULT_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": build_consult_user_prompt(role, envelope.payload or {}),
            },
        ]
        try:
            run = await run_agent(
                backend=self.backend,
                model=model,
                messages=messages,
                tools=[],
                max_steps=0,
                execute_tool=_no_tool,
                temperature=0.2,
                num_ctx=self.settings.num_ctx,
                num_predict=self.settings.num_predict,
            )
        except Exception as exc:  # noqa: BLE001
            return ResultPayload(ok=False, error=f"agent error: {exc}")
        try:
            answer = ConsultAnswer.model_validate(extract_json(run.content))
        except (ValueError, ValidationError) as exc:
            return ResultPayload(ok=False, error=f"invalid consult answer: {exc}")
        log.info(
            "consult %s -> %s: %s",
            request.question[:60],
            role.value,
            answer.answer[:80] if answer.answer else answer.cannot_answer[:80],
        )
        return ResultPayload(
            ok=True,
            output={"consult": answer.model_dump()},
            prompt_version=PROMPT_VERSION,
        )

    async def _run_impl(self, envelope: Envelope, snapshot: TaskSnapshot) -> ResultPayload:
        role = envelope.role or self.role
        model = self.settings.model_for(role)
        # M16 mesh-clarify: this command is a peer consult, not a role run.
        consult = (envelope.payload or {}).get("consult")
        if consult:
            return await self._run_consult(envelope, role, model, consult)
        # Harness layer (14.14): external harnesses take the coder role via
        # the generic Harness contract; "inproc" keeps the built-in loop.
        if role is Role.CODER and self.settings.harness_coder != "inproc":
            return await self._run_harness(envelope, role, model)
        self._written_files = {}
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
                return ResultPayload(ok=False, error=f"invalid {role.value} output: {exc}")

        if role is Role.CODER and self.settings.apply_artifacts:
            merged = merge_written_files(output, self._written_files)
            if not output.files and merged:
                output.files = [
                    FileArtifact(path=path, content=content) for path, content in merged.items()
                ]
            errors = python_syntax_errors(merged)
            if errors:
                return ResultPayload(
                    ok=False,
                    error="python syntax error: " + "; ".join(errors),
                    tool_steps=run.tool_steps,
                )
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

    async def _run_harness(self, envelope: Envelope, role: Role, model: str) -> ResultPayload:
        """Run the coder role through an external harness (workspace state).

        The harness edits the workspace directly; we harvest the changed
        files back into CoderOutput so the orchestrator/critic contract is
        unchanged (14.14).
        """
        if role is not Role.CODER:
            return ResultPayload(
                ok=False,
                error=f"harness {self.settings.harness_coder!r} runs the coder role only",
            )
        prompt = build_user_prompt(role, envelope.payload or {})
        harness = get_harness(self.settings.harness_coder, model=model)
        workspace = Path(self.settings.workspace_root)
        try:
            result = await harness.run(
                prompt, workspace, timeout_s=self.settings.coder_timeout_s
            )
        except Exception as exc:  # noqa: BLE001
            return ResultPayload(ok=False, error=f"harness error: {exc}")
        if not result.ok:
            return ResultPayload(
                ok=False,
                error=result.error or f"harness {self.settings.harness_coder} failed",
                tool_steps=result.tool_events,
            )
        changed = await changed_file_contents(
            workspace, max_bytes=self.settings.max_artifact_bytes
        )
        errors = python_syntax_errors(changed)
        if errors:
            return ResultPayload(
                ok=False,
                error="python syntax error: " + "; ".join(errors),
                tool_steps=result.tool_events,
            )
        output = CoderOutput(
            summary=result.summary,
            files=[FileArtifact(path=path, content=content) for path, content in changed.items()],
        )
        return ResultPayload(
            ok=True,
            output=output.model_dump(),
            prompt_version=PROMPT_VERSION,
            tool_steps=result.tool_events,
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

    def _note_written(self, name: str, arguments: dict, output: str) -> None:
        if name != "write_file":
            return
        try:
            result = json.loads(output)
        except ValueError:
            return
        if result.get("ok") and "path" in arguments and "content" in arguments:
            self._written_files[str(arguments["path"])] = str(arguments["content"])

    async def _execute_tool(self, envelope: Envelope, name: str, arguments: dict) -> str:
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
            replayed = snapshot.tool_results[key]
            self._note_written(name, arguments, replayed)
            return replayed

        output = await self.tools.call(name, arguments)
        output = output[:MAX_TOOL_RESULT_CHARS]
        self._note_written(name, arguments, output)

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
        return json.dumps({"ok": True, "task_id": data.get("task_id"), "depth": depth})

    # -- M11: ledger-based self-claiming -------------------------------------
    async def _propose_retirement(self, model: str, rep) -> None:
        """T7: raise a human-gated retirement proposal for a rejected model."""
        request = ModelRequest(
            request_id=new_id(),
            model=model,
            reason=(
                f"retirement proposal: {rep.consecutive_rejections} consecutive "
                f"critic rejections ({rep.rejections} total, {rep.approvals} "
                "approvals)"
            ),
            action="retire",
            decided_by=self.settings.node_name,
        )
        await self.bus.put_model_request(request)
        log.info(
            "retirement proposed",
            extra={"model": model, "request": request.request_id},
        )

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
                # T4: a paused node pulls no work (flag survives heartbeat
                # refreshes; cleared by resume_node or a process restart).
                if await self._paused():
                    continue
                # 14.13 step 5: experience-driven claim gate. One shot per
                # poll: aggregate the deposited observations and withdraw
                # from claiming when OUR track record on this role is below
                # threshold (min-samples guard; CLAIM_SYNERGY_GATE switch).
                # No central scheduler — the node reads its own history and
                # politely steps back, leaving work to peers with better
                # track records on this role.
                if self.settings.claim_synergy_gate:
                    _stats = compute_server_success(
                        await self.bus.list_observations()
                    )
                    _server = (
                        f"{self.settings.node_name}:"
                        f"{self.settings.model_for(self.role)}"
                    )
                    if should_withdraw(
                        _stats,
                        _server,
                        min_samples=self.settings.claim_synergy_min_samples,
                        min_ratio=self.settings.claim_synergy_min_ratio,
                    ):
                        log.info(
                            "synergy gate: withdrawing from claims "
                            "(track record below threshold)",
                            extra={"server": _server},
                        )
                        continue
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
                    # T7: a retired model is not claimable through — the node
                    # that retired it unloads it, so planning around it would
                    # hand the task to a node that cannot run it.
                    needs = snapshot.needs
                    if needs and needs.model:
                        rep = await self.bus.get_reputation(needs.model)
                        if rep.retired:
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
                    "critique": snapshot.critique.model_dump() if snapshot.critique else None,
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
            except TimeoutError:
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
            await self.bus.publish_envelope(SUBJECT_RESULT.format(role=role.value), result_envelope)
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
        # T4 (model-routing-visible): record which node:model ran this step.
        if envelope.role is not None and result.model:
            node = envelope.node or self.settings.node_name
            snapshot.route[envelope.role.value] = f"{node}:{result.model}"

        event = Event("result", ok=result.ok, reason=result.error or "")
        if result.ok and envelope.role is not None:
            try:
                output_model = _OUTPUT_SCHEMA[envelope.role]
                output = output_model.model_validate(result.output)
                self._store_output(snapshot, envelope.role, output)
                question = snapshot.question_output(envelope.role)
                if question:
                    event = Event("ask", reason=question, role=envelope.role)
                # T5: stream the coder/critic debate as events + history
                await emit_debate(self.bus, snapshot, envelope.role)
            except Exception as exc:  # noqa: BLE001
                log.warning("bad output in self pipeline: %s", exc)
                event = Event("result", ok=False, reason=f"invalid output: {exc}")
            if (
                envelope.role is Role.CRITIC
                and snapshot.critique is not None
                and not snapshot.critique.question
            ):
                # T7: record the verdict against the coder model (reputation)
                # and raise a retirement proposal once the streak is long.
                coder_model = self.settings.model_coder
                try:
                    rep = await self.bus.record_reputation(coder_model, snapshot.critique.approved)
                    if should_propose_retirement(rep, self.settings.retire_threshold):
                        await self._propose_retirement(coder_model, rep)
                except Exception:  # noqa: BLE001 - reputation is best effort
                    log.warning("reputation recording failed", exc_info=True)
                await self._verify_gate(snapshot)
                event = Event("result", ok=True, approved=snapshot.critique.approved)
        if envelope.node:
            await self.bus.emit_event(snapshot, "result.executed_on", envelope.node)
        await self._apply_local(snapshot, event)

    async def _verify_gate(self, snapshot: TaskSnapshot) -> None:
        """M14 deterministic verification gate.

        Small-model critics hallucinate blockers on correct code. This gate
        runs mechanical checks on the written artifacts: py_compile every
        Python file and, if there is exactly one, execute it. Evidence beats
        the LLM verdict in both directions:
        - checks pass and the critic rejected -> override to approved
        - checks fail -> replace the critic's issues with the real errors
        """
        critique = snapshot.critique
        if critique is None:
            return
        if critique.approved:
            return
        files = snapshot.code.files if snapshot.code else []
        py_files = [f for f in files if f.path.endswith(".py")]
        if not py_files:
            return
        root = Path(self.settings.workspace_root)
        evidence: list[str] = []
        passed = True
        for f in py_files:
            path = root / f.path
            if not path.is_file():
                evidence.append(f"py_compile {f.path}: MISSING FILE")
                passed = False
                continue
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "py_compile",
                str(path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            except TimeoutError:
                proc.kill()
                evidence.append(f"py_compile {f.path}: TIMEOUT")
                passed = False
                continue
            text = out.decode(errors="replace").strip()
            evidence.append(
                f"py_compile {f.path}: exit={proc.returncode}" + (f" {text[:200]}" if text else "")
            )
            if proc.returncode != 0:
                passed = False
        if passed and len(py_files) == 1:
            path = root / py_files[0].path
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                str(path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            except TimeoutError:
                proc.kill()
                evidence.append(f"run {py_files[0].path}: TIMEOUT")
                passed = False
            else:
                text = out.decode(errors="replace").strip()
                evidence.append(
                    f"run {py_files[0].path}: exit={proc.returncode}"
                    + (f" output: {text[:300]}" if text else "")
                )
                if proc.returncode != 0:
                    passed = False
        joined = "\n".join(evidence)
        log.info("verify gate: passed=%s evidence=%s", passed, joined)
        if passed:
            critique.approved = True
            critique.summary = (
                "[verify-gate] deterministic checks passed; LLM critique overridden.\n" + joined
            )
            critique.issues = []
        else:
            critique.issues = [
                CriticIssue(
                    severity="blocker",
                    issue=f"[verify-gate] deterministic verification failed:\n{joined}",
                    fix="fix the reported runtime/compile errors",
                )
            ]

    def _store_output(self, snapshot: TaskSnapshot, role: Role, output) -> None:
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
                context=f"fanout parent={snapshot.task_id} step={step.id}: {snapshot.task[:120]}",
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

    async def _apply_local(self, snapshot: TaskSnapshot, event: Event) -> None:
        result = transition(
            snapshot.state,
            event,
            attempt=snapshot.attempt,
            revisions=snapshot.revisions,
            max_attempts=self.settings.max_attempts,
            max_revisions=snapshot.max_revisions,
            gate=snapshot.gate,
        )
        snapshot.state = result.state
        snapshot.attempt = 0 if result.attempt_reset else snapshot.attempt + result.attempt_delta
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
            elif action.type == "ask":
                # M14 handoff: park until a human answers via meshctl answer.
                snapshot.next_role = ""
                snapshot.claimable = False
                snapshot.question = action.reason
                snapshot.question_role = action.role
                snapshot.record("task.question", action.reason)
                await self.bus.emit_event(
                    snapshot,
                    "task.question",
                    f"question: {action.reason} — /answer {snapshot.task_id} <text>",
                )
                # M16 mesh-clarify: consult the peers before the human.
                await clarify.start(self.bus, self.settings, snapshot)
            elif action.type == "park":
                # T3 approval gate: park at CRITIQUE for a human decision.
                snapshot.needs_human = True
                snapshot.next_role = ""
                snapshot.claimable = False
                snapshot.record("task.parked", action.reason)

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
        await self.bus.emit_event(snapshot, f"state.{result.state.value.lower()}", event.reason)
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
