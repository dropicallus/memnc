import asyncio
import json
import os
import signal

import pytest

from memnc.config import Settings
from memnc.mcp.client import MCPClient


@pytest.mark.asyncio
async def test_mcp_stdio_roundtrip(tmp_path):
    settings = Settings(
        workspace_root=tmp_path,
        allow_shell=True,
        shell_timeout_s=10,
        mcp_enabled=True,
    )
    client = MCPClient(settings)
    await client.start()
    try:
        assert client.available
        names = {schema["function"]["name"] for schema in client.schemas}
        assert {
            "read_file",
            "write_file",
            "list_dir",
            "search_text",
            "run_shell",
        } <= names

        wrote = json.loads(await client.call("write_file", {"path": "hello.txt", "content": "hi"}))
        assert wrote["ok"] is True

        read = json.loads(await client.call("read_file", {"path": "hello.txt"}))
        assert read["content"] == "hi"

        shell = json.loads(await client.call("run_shell", {"cmd": "cat hello.txt"}))
        assert shell["ok"] is True
        assert shell["stdout"] == "hi"

        escape = json.loads(
            await client.call("write_file", {"path": "../escape.txt", "content": "no"})
        )
        assert escape["ok"] is False
    finally:
        await client.close()


def _mcp_children() -> list[int]:
    """PIDs of memnc.mcp.server subprocesses owned by this test process."""
    me = os.getpid()
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", "rb") as fh:
                stat = fh.read().decode("utf-8", "replace")
            ppid = int(stat.rsplit(")", 1)[1].split()[1])
            if ppid != me:
                continue
            with open(f"/proc/{entry}/cmdline", "rb") as fh:
                cmdline = fh.read().replace(b"\x00", b" ").decode("utf-8", "replace")
        except (OSError, IndexError, ValueError):
            continue
        if "memnc.mcp.server" in cmdline:
            found.append(int(entry))
    return found


@pytest.mark.asyncio
async def test_mcp_client_reconnects_after_server_death(tmp_path):
    """A dead stdio subprocess must not wedge the lane (OOM-kill regression)."""
    settings = Settings(
        workspace_root=tmp_path,
        allow_shell=True,
        shell_timeout_s=10,
        mcp_enabled=True,
    )
    (tmp_path / "note.txt").write_text("hi", encoding="utf-8")
    client = MCPClient(settings)
    await client.start()
    try:
        assert client.available
        assert json.loads(await client.call("read_file", {"path": "note.txt"}))["content"] == "hi"

        victims = _mcp_children()
        assert victims, "expected a memnc.mcp.server child process"
        for pid in victims:
            os.kill(pid, signal.SIGKILL)
        await asyncio.sleep(0.5)

        # The first call hits the dead transport, restarts the server and replays.
        recovered = json.loads(await client.call("read_file", {"path": "note.txt"}))
        assert recovered["ok"] is True
        assert recovered["content"] == "hi"
        assert client.restarts >= 1
        assert client.available
    finally:
        await client.close()
