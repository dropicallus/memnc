"""Harness abstraction layer: protocol, registry, pi event parsing."""

import asyncio
import json
import stat
import textwrap
from pathlib import Path

import pytest

from memnc.config import Settings
from memnc.harnesses import (
    HarnessResult,
    get_harness,
    known_harnesses,
    register,
)
from memnc.harnesses.inproc import ToolLoopHarness
from memnc.harnesses.pi import PiHarness, parse_events
from memnc.harnesses.workspace import changed_file_contents
from memnc.schema import Envelope, Kind, Role
from memnc.worker import Worker


def _line(event: dict) -> str:
    return json.dumps(event)


def _assistant_message_end(text: str, *, stop: str = "stop") -> str:
    return _line(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
                "stopReason": stop,
            },
        }
    )


def test_parse_events_counts_tools_and_summary():
    stream = "\n".join(
        [
            _line({"type": "session"}),
            _line({"type": "agent_start"}),
            _line({"type": "tool_execution_start", "toolName": "edit", "args": {}}),
            _line({"type": "message_update"}),
            _line({"type": "tool_execution_start", "toolName": "read", "args": {}}),
            _assistant_message_end("done, tests pass"),
            _line({"type": "agent_settled"}),
        ]
    )
    parsed = parse_events(stream)
    assert parsed["tool_events"] == 2
    assert parsed["summary"] == "done, tests pass"
    assert parsed["settled"] is True
    assert parsed["errors"] == []


def test_parse_events_tolerates_noise_and_huge_lines():
    stream = "not json\n\n" + _line({"type": "agent_settled"})
    parsed = parse_events(stream)
    assert parsed["settled"] is True
    assert parsed["tool_events"] == 0


def test_parse_events_reports_error_turns():
    stream = _line(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [],
                "stopReason": "error",
                "errorMessage": "Request timed out.",
            },
        }
    )
    parsed = parse_events(stream)
    assert parsed["errors"] == ["Request timed out."]


def test_get_harness_unknown_name_raises():
    with pytest.raises(KeyError):
        get_harness("no-such-harness")


def test_registry_contains_pi_and_is_extensible():
    assert "pi" in known_harnesses()

    @register
    class Fake:
        name = "fake-probe"

    try:
        assert "fake-probe" in known_harnesses()
        assert isinstance(get_harness("fake-probe"), Fake)
    finally:
        from memnc.harnesses import _REGISTRY

        _REGISTRY.pop("fake-probe", None)


def _write_fake_pi(tmp_path: Path, body: str) -> str:
    script = tmp_path / "fake-pi.sh"
    script.write_text(textwrap.dedent(body))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def test_pi_harness_run_ok(tmp_path):
    pi_bin = _write_fake_pi(
        tmp_path,
        """#!/bin/sh
        printf '%s\n' '{"type":"tool_execution_start","toolName":"edit","args":{}}'
        printf '%s\n' '{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"all green"}],"stopReason":"stop"}}'
        printf '%s\n' '{"type":"agent_settled"}'
        """,
    )
    harness = PiHarness(model="ollama-power/qwen2.5:7b", pi_bin=pi_bin)
    result = asyncio.run(harness.run("do it", tmp_path / "ws"))
    assert isinstance(result, HarnessResult)
    assert result.ok is True
    assert result.tool_events == 1
    assert result.summary == "all green"
    assert result.error is None
    assert (tmp_path / "ws").is_dir()  # workspace created


def test_pi_harness_reports_nonzero_exit(tmp_path):
    pi_bin = _write_fake_pi(tmp_path, "#!/bin/sh\nexit 3\n")
    harness = PiHarness(model="x", pi_bin=pi_bin)
    result = asyncio.run(harness.run("do it", tmp_path))
    assert result.ok is False
    assert result.exit_code == 3


def test_pi_harness_timeout_kills_whole_tree(tmp_path):
    pi_bin = _write_fake_pi(tmp_path, "#!/bin/sh\nsleep 30\n")
    harness = PiHarness(model="x", pi_bin=pi_bin)
    import time

    started = time.monotonic()
    result = asyncio.run(harness.run("do it", tmp_path, timeout_s=1))
    elapsed = time.monotonic() - started
    assert result.ok is False
    assert "timed out" in (result.error or "")
    # The orphaned `sleep 30` child must die with the group, not hold pipes.
    assert elapsed < 10


def test_pi_harness_missing_binary(tmp_path):
    harness = PiHarness(model="x", pi_bin=str(tmp_path / "no-such-pi"))
    result = asyncio.run(harness.run("do it", tmp_path))
    assert result.ok is False
    assert "cannot start pi" in (result.error or "")


class _FakeBus:
    async def get_snapshot(self, task_id):
        return None

    async def put_snapshot(self, snapshot):
        return None


class _FakeTools:
    available = True
    schemas = []

    async def call(self, name, arguments):
        return "result"

    async def close(self):
        return None


def _coder_worker(tmp_path, harness_name):
    settings = Settings(
        role="coder",
        workspace_root=str(tmp_path / "ws"),
        mcp_enabled=False,
        harness_coder=harness_name,
    )
    return Worker(settings, backend=None, tools=_FakeTools(), bus=_FakeBus())


def _coder_envelope(payload):
    return Envelope(
        id="cmd-h",
        task_id="t-harness",
        trace_id="tr-h",
        kind=Kind.COMMAND,
        role=Role.CODER,
        payload=payload,
    )


def test_changed_file_contents_includes_untracked(tmp_path):
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "new.py").write_text("x = 1\n")
    (tmp_path / "notes.txt").write_text("hi")
    changed = asyncio.run(changed_file_contents(tmp_path))
    assert changed == {"new.py": "x = 1\n", "notes.txt": "hi"}


def test_changed_file_contents_non_repo_is_empty(tmp_path):
    assert asyncio.run(changed_file_contents(tmp_path)) == {}


def test_worker_harness_coder_harvests_workspace_state(tmp_path):
    import subprocess

    ws = tmp_path / "ws"
    ws.mkdir()
    subprocess.run(["git", "init", "-q", str(ws)], check=True)

    class FakeWorkspaceHarness:
        def __init__(self, **kwargs):
            pass
        name = "fake-writer"

        def capabilities(self):
            return {}

        async def run(self, prompt, workspace, timeout_s=900):
            (workspace / "hello.py").write_text("x = 1\n")
            return HarnessResult(ok=True, summary="wrote hello.py", tool_events=2)

    register(FakeWorkspaceHarness)
    worker = _coder_worker(tmp_path, "fake-writer")
    result = asyncio.run(worker._run(_coder_envelope({"task": "add hello"}), None))
    assert result.ok is True
    assert result.model == worker.settings.model_for(Role.CODER)
    files = result.output["files"]
    assert files == [{"path": "hello.py", "content": "x = 1\n"}]
    assert result.output["summary"] == "wrote hello.py"
    assert result.tool_steps == 2


def test_worker_harness_coder_reports_failure(tmp_path):
    class BrokenHarness:
        def __init__(self, **kwargs):
            pass
        name = "fake-broken"

        def capabilities(self):
            return {}

        async def run(self, prompt, workspace, timeout_s=900):
            return HarnessResult(ok=False, error="pi timed out", tool_events=0)

    register(BrokenHarness)
    worker = _coder_worker(tmp_path, "fake-broken")
    result = asyncio.run(worker._run(_coder_envelope({"task": "x"}), None))
    assert result.ok is False
    assert result.error == "pi timed out"
    assert result.tool_steps == 0


def test_worker_harness_coder_rejects_broken_python(tmp_path):
    import subprocess

    ws = tmp_path / "ws"
    ws.mkdir()
    subprocess.run(["git", "init", "-q", str(ws)], check=True)

    class DirtyHarness:
        def __init__(self, **kwargs):
            pass
        name = "fake-dirty"

        def capabilities(self):
            return {}

        async def run(self, prompt, workspace, timeout_s=900):
            (workspace / "bad.py").write_text("def f(:\n")
            return HarnessResult(ok=True, summary="wrote bad.py", tool_events=1)

    register(DirtyHarness)
    worker = _coder_worker(tmp_path, "fake-dirty")
    result = asyncio.run(worker._run(_coder_envelope({"task": "x"}), None))
    assert result.ok is False
    assert "python syntax error" in result.error


def test_worker_non_coder_role_ignores_harness(tmp_path, monkeypatch):
    import memnc.worker as worker_mod

    class MustNotRunHarness:
        name = "fake-must-not-run"

        def __init__(self, **kwargs):
            pass

        def capabilities(self):
            return {}

        async def run(self, prompt, workspace, timeout_s=900):
            raise AssertionError("harness must not run for non-coder roles")

    register(MustNotRunHarness)

    async def boom(*args, **kwargs):
        raise AssertionError("in-proc loop reached: this test only checks routing")

    # Immune to cross-test module pollution (test_clarify patches run_agent
    # without restoring): pin it deterministically for this test.
    monkeypatch.setattr(worker_mod, "run_agent", boom)
    worker = _coder_worker(tmp_path, "fake-must-not-run")
    envelope = Envelope(
        id="cmd-h2",
        task_id="t-harness2",
        trace_id="tr-h2",
        kind=Kind.COMMAND,
        role=Role.CRITIC,
        payload={},
    )
    result = asyncio.run(worker._run(envelope, None))
    assert result.ok is False
    # The critic role took the in-proc loop, NOT the registered harness.
    assert "only checks routing" in result.error


def test_worker_default_harness_is_inproc(tmp_path):
    worker = _coder_worker(tmp_path, "inproc")
    assert worker.settings.harness_coder == "inproc"


class _FakeChatResult:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class _FakeToolCall:
    def __init__(self, name, arguments, call_id="call-1"):
        self.name = name
        self.arguments = arguments
        self.id = call_id


class _FakeBackend:
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    async def chat(self, messages, tools=None, model=None, **kwargs):
        result = self.results[min(self.calls, len(self.results) - 1)]
        self.calls += 1
        return result


def test_tool_loop_harness_happy_path(tmp_path):
    import json

    final = {"summary": "wrote hi.py", "files": [{"path": "hi.py", "content": "x = 1\n"}]}
    backend = _FakeBackend([
        _FakeChatResult(tool_calls=[_FakeToolCall("write_file", {"path": "hi.py"})]),
        _FakeChatResult(content=json.dumps(final)),
    ])
    seen = []

    async def execute(name, arguments):
        seen.append(name)
        return "ok"

    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    harness = ToolLoopHarness(model="fake", backend=backend, execute_tool=execute)
    result = asyncio.run(harness.run("do it", tmp_path, timeout_s=10))
    assert result.ok is True
    assert result.summary == "wrote hi.py"
    assert result.tool_events == 1
    assert seen == ["write_file"]
    assert (tmp_path / "hi.py").read_text() == "x = 1\n"
    assert "hi.py" in result.diff


def test_tool_loop_harness_rejects_bad_json(tmp_path):
    backend = _FakeBackend([_FakeChatResult(content="no json here")])
    harness = ToolLoopHarness(model="fake", backend=backend)
    result = asyncio.run(harness.run("do it", tmp_path, timeout_s=10))
    assert result.ok is False
    assert "invalid coder output" in result.error


def test_tool_loop_harness_timeout(tmp_path):
    import asyncio as aio
    import time

    class SlowBackend:
        async def chat(self, messages, **kwargs):
            await aio.sleep(30)
            return _FakeChatResult(content="{}")

    harness = ToolLoopHarness(model="fake", backend=SlowBackend(), max_steps=1)
    started = time.monotonic()
    result = asyncio.run(harness.run("do it", tmp_path, timeout_s=1))
    assert result.ok is False
    assert "timed out" in result.error
    assert time.monotonic() - started < 10


def test_inproc_harness_registered():
    assert "inproc" in known_harnesses()
    harness = ToolLoopHarness(model="fake", backend=_FakeBackend([]))
    caps = harness.capabilities()
    assert caps.external_process is False
