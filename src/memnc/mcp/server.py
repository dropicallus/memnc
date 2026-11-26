"""MCP stdio server exposing local filesystem and shell tools."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from ..config import Settings
from .tools_fs import list_dir as _list_dir
from .tools_fs import read_file as _read_file
from .tools_fs import search_text as _search_text
from .tools_fs import write_file as _write_file
from .tools_monitor import daemon_status as _daemon_status
from .tools_monitor import gpu_status as _gpu_status
from .tools_monitor import inference_status as _inference_status
from .tools_monitor import run_status as _run_status
from .tools_shell import run_shell as _run_shell

log = logging.getLogger(__name__)


def create_server(
    workspace: Path,
    *,
    allow_shell: bool = True,
    shell_timeout_s: int = 120,
    max_output_bytes: int = 65_536,
) -> MCPServer:
    workspace = Path(workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    server = MCPServer("memnc-tools")

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
    def gpu_status() -> dict:
        """Per-GPU used/total VRAM on this node."""
        return _gpu_status()

    @server.tool()
    def inference_status() -> dict:
        """Both inference lanes: ollama :11434 and llama-server :8098."""
        return _inference_status()

    @server.tool()
    def run_status(pattern: str, log: str = "", ckpt: str = "") -> dict:
        """Long-running job status: process liveness + checkpoint progress + log tail."""
        return _run_status(pattern, log=log or None, ckpt=ckpt or None, root=workspace)

    @server.tool()
    def daemon_status(units: str = "memnc-backlog-driver") -> dict:
        """systemd --user unit states (comma-separated unit names)."""
        return _daemon_status(units)

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
