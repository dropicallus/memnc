import json

import pytest

from memnc.mcp.tools_fs import list_dir, read_file, search_text, write_file
from memnc.mcp.tools_shell import minimal_env, run_shell


def test_write_then_read(tmp_path):
    assert json.loads(write_file(tmp_path, "a/b.txt", "hello"))["ok"]
    result = json.loads(read_file(tmp_path, "a/b.txt"))
    assert result["content"] == "hello"
    assert result["truncated"] is False


def test_read_missing_file_reports_error(tmp_path):
    assert json.loads(read_file(tmp_path, "nope.txt"))["ok"] is False


def test_write_outside_workspace_is_rejected(tmp_path):
    result = json.loads(write_file(tmp_path, "../escape.txt", "x"))
    assert result["ok"] is False


def test_list_dir_filters_prefix(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x")
    (tmp_path / "other.py").write_text("y")
    files = json.loads(list_dir(tmp_path, "src"))["files"]
    assert files == ["src/a.py"]


def test_search_text(tmp_path):
    (tmp_path / "a.py").write_text("import os\nVALUE = 42\n")
    result = json.loads(search_text(tmp_path, r"VALUE\s*=\s*42"))
    assert result["ok"] is True
    assert result["matches"][0]["line"] == 2


@pytest.mark.asyncio
async def test_shell_runs_in_workspace(tmp_path):
    (tmp_path / "here.txt").write_text("x")
    result = json.loads(await run_shell(tmp_path, "cat here.txt", timeout_s=5, allow_shell=True))
    assert result["ok"] is True
    assert result["stdout"] == "x"
    assert result["exit_code"] == 0


@pytest.mark.asyncio
async def test_shell_timeout_is_killed(tmp_path):
    result = json.loads(await run_shell(tmp_path, "sleep 5", timeout_s=1, allow_shell=True))
    assert result["timed_out"] is True
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_shell_disabled(tmp_path):
    result = json.loads(await run_shell(tmp_path, "echo hi", allow_shell=False))
    assert result["ok"] is False
    assert "disabled" in result["error"]


@pytest.mark.asyncio
async def test_shell_rejects_cwd_escape(tmp_path):
    result = json.loads(await run_shell(tmp_path, "ls", cwd="..", allow_shell=True))
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_shell_truncates_output(tmp_path):
    result = json.loads(await run_shell(tmp_path, "printf 'x%.0s' {1..100}", max_output_bytes=10))
    assert result["truncated"] is True
    assert result["stdout"] == "x" * 10


@pytest.mark.asyncio
async def test_shell_output_survives_many_reads(tmp_path):
    cmd = "for i in $(seq 1 2000); do echo line$i; sleep 0.001; done"
    result = json.loads(await run_shell(tmp_path, cmd, max_output_bytes=4_096))
    assert result["truncated"] is True
    assert len(result["stdout"]) == 4_096
    assert result["stdout"].startswith("line1\nline2\n")


@pytest.mark.asyncio
async def test_shell_small_output_is_not_truncated(tmp_path):
    cmd = "for i in $(seq 1 50); do echo line$i; sleep 0.001; done"
    result = json.loads(await run_shell(tmp_path, cmd, max_output_bytes=65_536))
    assert result["truncated"] is False
    assert result["stdout"].splitlines()[-1] == "line50"


def test_minimal_env_drops_secrets(monkeypatch):
    monkeypatch.setenv("NATS_TOKEN", "secret-token")
    monkeypatch.setenv("INFERENCE_API_KEY", "secret-key")
    env = minimal_env()
    assert "NATS_TOKEN" not in env
    assert "INFERENCE_API_KEY" not in env
    assert "PATH" in env


def test_run_status_refuses_paths_outside_workspace(tmp_path):
    from memnc.mcp.tools_monitor import run_status

    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside.log"
    outside.write_text("TOKEN=abc\n")
    (ws / "escape.log").symlink_to(outside)
    result = run_status("memnc-no-such-process-xyz", log=str(outside), root=ws)
    assert "rejected" in result["log_tail"]
    result = run_status("memnc-no-such-process-xyz", log="escape.log", root=ws)
    assert "rejected" in result["log_tail"]


def test_run_status_reads_log_inside_workspace(tmp_path):
    from memnc.mcp.tools_monitor import run_status

    (tmp_path / "run.log").write_text("a\nb\n")
    result = run_status("memnc-no-such-process-xyz", log="run.log", root=tmp_path)
    assert result["log_tail"] == ["a", "b"]
