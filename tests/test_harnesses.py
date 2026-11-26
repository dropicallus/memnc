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
from memnc.harnesses.opencode import stage_config
from memnc.harnesses.pi import PiHarness, parse_events
from memnc.harnesses.workspace import changed_file_contents
from memnc.schema import Envelope, Kind, Role
from memnc.worker import Worker


def test_stage_config_copies_node_config_into_writable_home(tmp_path, monkeypatch):
    source = tmp_path / "node-config"
    source.mkdir()
    (source / "opencode.json").write_text('{"provider": {}}')
    monkeypatch.setenv("OPENCODE_CONFIG_SOURCE", str(source))
    monkeypatch.setenv("OPENCODE_HOME", str(tmp_path / "home"))

    config_path, overlay = stage_config()

    assert Path(config_path).read_text() == '{"provider": {}}'
    assert overlay["OPENCODE_CONFIG"] == config_path
    # opencode writes next to its config, so the copy must not be the ro mount
    assert str(source) not in config_path


def test_stage_config_is_empty_without_a_node_config(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_CONFIG_SOURCE", str(tmp_path / "missing"))
    assert stage_config() == ("", {})


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


# --- 14.14 per-task harness selection: envelope payload overrides the node ---


def test_worker_payload_harness_overrides_node_default(tmp_path):
    seen_names = []

    class RecordingHarness:
        name = "fake-per-task"

        def __init__(self, **kwargs):
            seen_names.append(kwargs.get("model"))

        def capabilities(self):
            return {}

        async def run(self, prompt, workspace, timeout_s=900):
            return HarnessResult(ok=True, summary="done", tool_events=0)

    register(RecordingHarness)
    try:
        # Node default is "inproc", but the task payload names "fake-per-task".
        worker = _coder_worker(tmp_path, "inproc")
        result = asyncio.run(
            worker._run(_coder_envelope({"task": "x", "harness": "fake-per-task"}), None)
        )
    finally:
        from memnc.harnesses import _REGISTRY

        _REGISTRY.pop("fake-per-task", None)
    assert result.ok is True
    assert seen_names  # the payload-named harness ran, not the node default


def test_worker_payload_inproc_forces_builtin_loop(tmp_path, monkeypatch):
    import memnc.worker as worker_mod

    class MustNotRunHarness:
        name = "fake-bypassed"

        def __init__(self, **kwargs):
            pass

        def capabilities(self):
            return {}

        async def run(self, prompt, workspace, timeout_s=900):
            raise AssertionError("payload harness=inproc must use the builtin loop")

    register(MustNotRunHarness)
    try:
        async def boom(*args, **kwargs):
            raise AssertionError("builtin loop reached")

        monkeypatch.setattr(worker_mod, "run_agent", boom)
        # Node default is an external harness; payload says inproc.
        worker = _coder_worker(tmp_path, "fake-bypassed")
        result = asyncio.run(
            worker._run(_coder_envelope({"task": "x", "harness": "inproc"}), None)
        )
    finally:
        from memnc.harnesses import _REGISTRY

        _REGISTRY.pop("fake-bypassed", None)
    assert result.ok is False
    # The builtin loop ran (its failure), NOT the registered harness.
    assert "builtin loop reached" in result.error


def test_worker_empty_payload_harness_falls_back_to_node_default(tmp_path):
    class DefaultHarness:
        name = "fake-default"

        def __init__(self, **kwargs):
            pass

        def capabilities(self):
            return {}

        async def run(self, prompt, workspace, timeout_s=900):
            return HarnessResult(ok=False, error="default ran", tool_events=0)

    register(DefaultHarness)
    try:
        worker = _coder_worker(tmp_path, "fake-default")
        result = asyncio.run(worker._run(_coder_envelope({"task": "x"}), None))
    finally:
        from memnc.harnesses import _REGISTRY

        _REGISTRY.pop("fake-default", None)
    assert result.ok is False
    assert result.error == "default ran"


# --- 15.4 OpenCode harness -------------------------------------------------


def test_opencode_registry_contains_opencode():
    assert "opencode" in known_harnesses()


def test_opencode_parse_counts_tool_markers_and_tail_summary():
    from memnc.harnesses.opencode import parse_output

    stream = (
        "build · ollama/mannix/qwen3.6-27b-a3b-coder:CD-IQ4_K_M\n"
        "← Write hello.py\n"
        "Wrote file successfully.\n"
        "\n"
        "$ python3 hello.py\n"
        "HELLO_FROM_OPENCODE\n"
    )
    parsed = parse_output(stream)
    assert parsed["tool_events"] == 2
    assert "HELLO_FROM_OPENCODE" in parsed["summary"]


def test_opencode_parse_tolerates_garbage():
    from memnc.harnesses.opencode import parse_output

    parsed = parse_output("random noise only, no markers\n")
    assert parsed["tool_events"] == 0
    assert parsed["summary"]


def _write_fake_opencode(tmp_path: Path, body: str) -> str:
    script = tmp_path / "fake-opencode.sh"
    script.write_text(textwrap.dedent(body))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def test_opencode_harness_run_ok(tmp_path):
    from memnc.harnesses.opencode import OpenCodeHarness

    oc_bin = _write_fake_opencode(
        tmp_path,
        """#!/bin/sh
        # args: run <prompt> --model <model>
        printf '%s\\n' '← Write hello.py'
        printf '%s\\n' '$ python3 hello.py'
        printf '%s\\n' 'HELLO'
        """,
    )
    harness = OpenCodeHarness(model="ollama/x", opencode_bin=oc_bin)
    result = asyncio.run(harness.run("do it", tmp_path / "ws"))
    assert result.ok is True
    assert result.tool_events == 2
    assert result.summary.endswith("HELLO")
    assert result.error is None
    assert (tmp_path / "ws").is_dir()


def test_opencode_qualify_model():
    from memnc.harnesses.opencode import qualify_model

    # bare ollama tag -> provider-qualified (worker MODEL_CODER case)
    assert qualify_model("mannix/qwen3.6-27b-a3b-coder:CD-IQ4_K_M") == (
        "ollama/mannix/qwen3.6-27b-a3b-coder:CD-IQ4_K_M"
    )
    # already qualified -> unchanged
    assert qualify_model("ollama/mannix/qwen3.6-27b-a3b-coder:CD-IQ4_K_M") == (
        "ollama/mannix/qwen3.6-27b-a3b-coder:CD-IQ4_K_M"
    )
    assert qualify_model("openai/gpt-4o") == "openai/gpt-4o"
    # empty -> unchanged (opencode falls back to its config default)
    assert qualify_model("") == ""
    # harness __init__ applies the mapping
    from memnc.harnesses.opencode import OpenCodeHarness

    assert OpenCodeHarness(model="x", opencode_bin="false").model == "ollama/x"


def test_opencode_qualify_model_keeps_configured_provider(tmp_path, monkeypatch):
    """A provider that exists only in the node's opencode.json counts as qualified.

    Regression: `inkling/inkling-2-mini` was rewritten to
    `ollama/inkling/inkling-2-mini`, which opencode rejects with a 500.
    """
    import json as _json

    from memnc.harnesses import opencode

    (tmp_path / "opencode.json").write_text(
        _json.dumps({"provider": {"inkling": {"models": {}}}})
    )
    monkeypatch.setenv("OPENCODE_CONFIG_SOURCE", str(tmp_path))
    monkeypatch.setattr(opencode, "_PROVIDER_CACHE", None)
    assert opencode.qualify_model("inkling/inkling-2-mini") == "inkling/inkling-2-mini"
    # unknown providers still get the ollama prefix
    assert opencode.qualify_model("nope/model") == "ollama/nope/model"


def test_opencode_stderr_error_lines_only():
    """Tool output on stderr is normal; only real error lines count."""
    from memnc.harnesses.opencode import _stderr_error

    assert _stderr_error("\x1b[0m") is None
    assert _stderr_error("hello\n") is None
    assert (
        _stderr_error('Error: {"name":"UnknownError","data":{"message":"x"}}')
        is not None
    )


def test_opencode_harness_reports_nonzero_exit(tmp_path):
    from memnc.harnesses.opencode import OpenCodeHarness

    oc_bin = _write_fake_opencode(tmp_path, "#!/bin/sh\nexit 3\n")
    harness = OpenCodeHarness(model="x", opencode_bin=oc_bin)
    result = asyncio.run(harness.run("do it", tmp_path))
    assert result.ok is False
    assert result.exit_code == 3


def test_opencode_harness_stderr_only_is_error(tmp_path):
    from memnc.harnesses.opencode import OpenCodeHarness

    oc_bin = _write_fake_opencode(
        tmp_path, "#!/bin/sh\necho 'Error: boom' >&2\nexit 1\n"
    )
    harness = OpenCodeHarness(model="x", opencode_bin=oc_bin)
    result = asyncio.run(harness.run("do it", tmp_path))
    assert result.ok is False
    assert result.error == "Error: boom"


def test_opencode_harness_ignores_tool_output_on_stderr(tmp_path):
    """A successful run whose tool printed to stderr must stay ok."""
    from memnc.harnesses.opencode import OpenCodeHarness

    oc_bin = _write_fake_opencode(tmp_path, "#!/bin/sh\necho hello >&2\nexit 0\n")
    harness = OpenCodeHarness(model="x", opencode_bin=oc_bin)
    result = asyncio.run(harness.run("do it", tmp_path))
    assert result.ok is True
    assert result.error is None


def test_opencode_harness_collects_files_without_git(tmp_path):
    """Artifacts come from a filesystem snapshot, not git (absent in the image)."""
    from memnc.harnesses.opencode import OpenCodeHarness

    oc_bin = _write_fake_opencode(
        tmp_path, "#!/bin/sh\nprintf \"print('hi')\\n\" > hello.py\necho done\n"
    )
    ws = tmp_path / "ws"
    ws.mkdir()
    harness = OpenCodeHarness(model="x", opencode_bin=oc_bin)
    result = asyncio.run(harness.run("do it", ws))
    assert result.ok is True
    assert result.files == {"hello.py": "print('hi')\n"}


def test_snapshot_tree_ignores_model_blobs(tmp_path):
    from memnc.harnesses.workspace import compare_trees, snapshot_tree

    (tmp_path / "ai_models").mkdir()
    (tmp_path / "ai_models" / "big.gguf").write_text("blob")
    before = snapshot_tree(tmp_path)
    assert before == {}
    (tmp_path / "out.py").write_text("x = 1\n")
    assert compare_trees(tmp_path, before) == {"out.py": "x = 1\n"}


def test_opencode_harness_timeout_kills_whole_tree(tmp_path):
    import time

    from memnc.harnesses.opencode import OpenCodeHarness

    oc_bin = _write_fake_opencode(tmp_path, "#!/bin/sh\nsleep 30\n")
    harness = OpenCodeHarness(model="x", opencode_bin=oc_bin)
    started = time.monotonic()
    result = asyncio.run(harness.run("do it", tmp_path, timeout_s=1))
    elapsed = time.monotonic() - started
    assert result.ok is False
    assert "timed out" in (result.error or "")
    assert elapsed < 10


def test_opencode_harness_missing_binary(tmp_path):
    from memnc.harnesses.opencode import OpenCodeHarness

    harness = OpenCodeHarness(model="x", opencode_bin=str(tmp_path / "no-such-oc"))
    result = asyncio.run(harness.run("do it", tmp_path))
    assert result.ok is False
    assert "cannot start opencode" in (result.error or "")


def test_opencode_harness_clean_state_flag(tmp_path, monkeypatch):
    from memnc.harnesses.opencode import OpenCodeHarness

    home = tmp_path / "home"
    state = home / ".local/share/opencode"
    state.mkdir(parents=True)
    (state / "session.db").write_text("x")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OPENCODE_CLEAN_STATE", "1")
    oc_bin = _write_fake_opencode(tmp_path, "#!/bin/sh\nexit 0\n")
    harness = OpenCodeHarness(model="x", opencode_bin=oc_bin)
    result = asyncio.run(harness.run("do it", tmp_path))
    assert result.ok is True
    assert not state.exists()  # wiped (and NOT recreated by the fake)


def test_workspace_listing_cases(tmp_path) -> None:
    from memnc.harnesses.workspace import workspace_listing

    assert workspace_listing(tmp_path / "missing") is None

    empty = tmp_path / "empty"
    empty.mkdir()
    assert workspace_listing(empty) is None

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "venv").mkdir()
    (ws / "report.md").write_text("x")
    listing = workspace_listing(ws)
    assert listing == "report.md\nvenv/"

    many = tmp_path / "many"
    many.mkdir()
    for i in range(30):
        (many / f"f{i:02d}").write_text("x")
    listing = workspace_listing(many, limit=10)
    lines = listing.splitlines()
    assert len(lines) == 11
    assert lines[-1] == "... and 20 more"


def test_build_user_prompt_workspace_state() -> None:
    from memnc.roles import Role, build_user_prompt

    prompt = build_user_prompt(Role.PLANNER, {"task": "t", "workspace_state": "venv/"})
    assert "WORKSPACE STATE" in prompt and "venv/" in prompt
    prompt = build_user_prompt(Role.PLANNER, {"task": "t"})
    assert "WORKSPACE STATE" not in prompt


def test_opencode_harness_does_not_inherit_secrets(tmp_path, monkeypatch):
    from memnc.harnesses.opencode import OpenCodeHarness

    monkeypatch.setenv("NATS_TOKEN", "leak-me")
    monkeypatch.setenv("INFERENCE_API_KEY", "leak-me-too")
    monkeypatch.setenv("MY_PROVIDER_KEY", "passed-on-purpose")
    monkeypatch.setenv("OPENCODE_ENV_PASS", "MY_PROVIDER_KEY")
    oc_bin = _write_fake_opencode(tmp_path, '#!/bin/sh\nenv > "$PWD/env.txt"\nexit 0\n')
    asyncio.run(OpenCodeHarness(model="x", opencode_bin=oc_bin).run("do it", tmp_path))
    dumped = (tmp_path / "env.txt").read_text()
    assert "NATS_TOKEN" not in dumped
    assert "INFERENCE_API_KEY" not in dumped
    assert "MY_PROVIDER_KEY=passed-on-purpose" in dumped
