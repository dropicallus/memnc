"""MCP stdio server exposing local filesystem and shell tools."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from ..config import Settings
from .tools_fs import list_dir as _list_dir
from .tools_fs import read_file as _read_file
from .tools_fs import search_text as _search_text
from .tools_fs import write_file as _write_file
from .tools_shell import run_shell as _run_shell

log = logging.getLogger(__name__)


def create_server(
    workspace: Path,
    *,
    allow_shell: bool = True,
    shell_timeout_s: int = 120,
    max_output_bytes: int = 65_536,
) -> FastMCP:
    workspace = Path(workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    server = FastMCP("memnc-tools")

    @server.tool()
    def read_file(path: str, max_bytes: int = 200_000) -> str:
        """Read a UTF-8 text file inside the workspace."""
        return _read_file(workspace, path, max_bytes)

    @server.tool()
    def write_file(path: str, content: str) -> str:
        """Write a UTF-8 text file inside the workspace, creating parents."""
        return _write_file(workspace, path, content)

    @server.tool()
    def list_dir(path: str = ".", depth: int = 2) -> str:
        """List workspace files relative to the workspace root."""
        return _list_dir(workspace, path, depth)

    @server.tool()
    def search_text(pattern: str, glob: str = "**/*", max_results: int = 100) -> str:
        """Regex-search text files in the workspace."""
        return _search_text(workspace, pattern, glob, max_results)

    @server.tool()
    async def run_shell(cmd: str, cwd: str = ".", timeout_s: int = 0) -> str:
        """Run a shell command inside the workspace (full shell, no allowlist)."""
        return await _run_shell(
            workspace,
            cmd,
            cwd=cwd,
            timeout_s=timeout_s,
            max_output_bytes=max_output_bytes,
            allow_shell=allow_shell,
        )

    return server


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = Settings()
    server = create_server(
        settings.workspace_root,
        allow_shell=settings.allow_shell,
        shell_timeout_s=settings.shell_timeout_s,
        max_output_bytes=settings.max_output_bytes,
    )
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
