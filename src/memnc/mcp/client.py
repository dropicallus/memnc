"""MCP stdio client used by workers."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ..config import Settings

log = logging.getLogger(__name__)

# Errors that mean "the stdio subprocess is gone" rather than "the tool said no".
# anyio raises these when the underlying pipe is closed by the child exiting;
# mcp wraps some of them into RuntimeError("Connection closed").
_TRANSPORT_HINTS = (
    "connection closed",
    "closedresource",
    "brokenresource",
    "endofstream",
    "broken pipe",
    "process exited",
    "server exited",
)


def _is_transport_error(exc: BaseException) -> bool:
    if isinstance(exc, (asyncio.CancelledError,)):
        return False
    try:
        import anyio

        if isinstance(exc, (anyio.ClosedResourceError, anyio.BrokenResourceError, anyio.EndOfStream)):
            return True
    except Exception:  # noqa: BLE001 - anyio is a hard dep via mcp, stay defensive
        pass
    for arg in getattr(exc, "exceptions", ()) or ():
        if _is_transport_error(arg):
            return True
    name = type(exc).__name__.lower()
    if "closedresource" in name or "brokenresource" in name or "endofstream" in name:
        return True
    text = str(exc).lower()
    return any(hint in text for hint in _TRANSPORT_HINTS)


class MCPClient:
    """Long-lived stdio MCP session plus cached tool schemas.

    The subprocess is a child of the worker and can die on its own (cgroup OOM
    kill, an operator pkill, a crashed tool). Without a reconnect the whole
    lane degrades to "Connection closed" on every tool call while the worker
    still reports ``available`` — which is exactly how a task can wedge in
    WAIT_INPUT claiming the workspace is empty. Every call therefore detects a
    dead transport, restarts the session once and replays the call.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self.schemas: list[dict[str, Any]] = []
        self._available = False
        self._lock: asyncio.Lock | None = None
        self.restarts = 0

    @property
    def available(self) -> bool:
        return self._available

    def _guard(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def start(self) -> None:
        # Minimal env: never pass the full os.environ to the MCP server
        # subprocess — it must not inherit NATS_TOKEN, INFERENCE_API_KEY etc.
        env = {
            k: os.environ[k]
            for k in ("PATH", "HOME", "LANG", "TMPDIR", "PYTHONPATH", "VIRTUAL_ENV")
            if k in os.environ
        }
        env.update(
            {
                "WORKSPACE_ROOT": str(Path(self.settings.workspace_root).resolve()),
                "ALLOW_SHELL": "true" if self.settings.allow_shell else "false",
                "SHELL_TIMEOUT_S": str(self.settings.shell_timeout_s),
                "MAX_OUTPUT_BYTES": str(self.settings.max_output_bytes),
            }
        )
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "memnc.mcp.server"],
            env=env,
        )
        self._stack = AsyncExitStack()
        try:
            read, write = await self._stack.enter_async_context(stdio_client(params))
            self._session = await self._stack.enter_async_context(ClientSession(read, write))
            await self._session.initialize()
            listing = await self._session.list_tools()
            self.schemas = [_to_openai_tool(tool) for tool in listing.tools]
            self._available = True
            log.info("mcp ready", extra={"tools": [s["function"]["name"] for s in self.schemas]})
        except Exception:
            log.exception("mcp server failed to start; continuing without tools")
            await self.close()

    async def restart(self) -> bool:
        """Tear the dead session down and start a fresh subprocess."""
        async with self._guard():
            await self.close()
            await self.start()
            if self._available:
                self.restarts += 1
                log.warning("mcp session restarted", extra={"restarts": self.restarts})
            return self._available

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        if self._session is None:
            # No session at all (start failed, or a previous restart failed):
            # one attempt to bring it up instead of failing the tool call.
            if not await self.restart():
                raise RuntimeError("mcp session is not available")
        try:
            return await self._call_once(name, arguments)
        except Exception as exc:  # noqa: BLE001 - filter below
            if not _is_transport_error(exc):
                raise
            log.warning(
                "mcp transport lost on %s (%s); reconnecting",
                name,
                exc,
            )
            if not await self.restart():
                raise RuntimeError(f"mcp session lost during {name} and restart failed") from exc
            # Replay once against the fresh subprocess.
            return await self._call_once(name, arguments)

    async def _call_once(self, name: str, arguments: dict[str, Any]) -> str:
        assert self._session is not None
        result = await self._session.call_tool(name, arguments)
        parts = [block.text for block in result.content if getattr(block, "type", None) == "text"]
        text = "\n".join(parts) if parts else ""
        if getattr(result, "isError", False):
            return f"tool error: {text}"
        return text

    async def close(self) -> None:
        self._available = False
        self._session = None
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except Exception:  # noqa: BLE001 - shutdown best effort
                log.debug("mcp shutdown raised", exc_info=True)
            self._stack = None


def _to_openai_tool(tool) -> dict[str, Any]:
    schema = getattr(tool, "inputSchema", None) or {
        "type": "object",
        "properties": {},
    }
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": schema,
        },
    }
