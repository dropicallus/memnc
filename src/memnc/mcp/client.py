"""MCP stdio client used by workers."""

from __future__ import annotations

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


class MCPClient:
    """Long-lived stdio MCP session plus cached tool schemas."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self.schemas: list[dict[str, Any]] = []
        self._available = False

    @property
    def available(self) -> bool:
        return self._available

    async def start(self) -> None:
        env = dict(os.environ)
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
            self._session = await self._stack.enter_async_context(
                ClientSession(read, write)
            )
            await self._session.initialize()
            listing = await self._session.list_tools()
            self.schemas = [_to_openai_tool(tool) for tool in listing.tools]
            self._available = True
            log.info("mcp ready", extra={"tools": [s["function"]["name"] for s in self.schemas]})
        except Exception:
            log.exception("mcp server failed to start; continuing without tools")
            await self.close()

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        if self._session is None:
            raise RuntimeError("mcp session is not available")
        result = await self._session.call_tool(name, arguments)
        parts = [
            block.text
            for block in result.content
            if getattr(block, "type", None) == "text"
        ]
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
