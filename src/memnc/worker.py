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
from .fsutil import apply_artifacts
from .mcp.client import MCPClient
from .roles import PROMPT_VERSION, SYSTEM_PROMPTS, build_user_prompt
from .schema import (
    Envelope,
    ResultPayload,
    Role,
    SUBJECT_HEARTBEAT,
    SUBJECT_RESULT,
    TaskSnapshot,
)

log = logging.getLogger(__name__)

MAX_TOOL_RESULT_CHARS = 8_000
MAX_CHECKPOINTS = 32
HEARTBEAT_INTERVAL_S = 5


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
        await asyncio.gather(self._command_loop(), self._heartbeat_loop())

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
            await self.bus.delete_tool_cache(envelope.task_id, envelope.id)
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
        if self.tools is None or not getattr(self.tools, "available", False):
            raise RuntimeError("no MCP tools available")

        key = tool_key(envelope.id, name, arguments)
        cache = await self.bus.get_tool_cache(
            envelope.task_id, envelope.id
        )
        if key in cache:
            log.info(
                "tool replay skipped",
                extra={"task_id": envelope.task_id, "tool": name},
            )
            return cache[key]

        output = await self.tools.call(name, arguments)
        output = output[:MAX_TOOL_RESULT_CHARS]

        cache = await self.bus.get_tool_cache(
            envelope.task_id, envelope.id
        )
        cache[key] = output
        while len(cache) > MAX_CHECKPOINTS:
            cache.pop(next(iter(cache)))
        await self.bus.put_tool_cache(
            envelope.task_id, envelope.id, cache
        )
        return output


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
