import json

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

        wrote = json.loads(
            await client.call("write_file", {"path": "hello.txt", "content": "hi"})
        )
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
