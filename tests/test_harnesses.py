"""Harness abstraction layer: protocol, registry, pi event parsing."""

import asyncio
import json
import stat
import textwrap
from pathlib import Path

import pytest

from memnc.harnesses import (
    HarnessResult,
    get_harness,
    known_harnesses,
    register,
)
from memnc.harnesses.pi import PiHarness, parse_events


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
