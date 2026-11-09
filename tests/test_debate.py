"""T5: debate streaming — coder/critic negotiation as events + history."""

import asyncio

from memnc.bus import emit_debate
from memnc.schema import (
    CoderOutput,
    CriticIssue,
    CriticOutput,
    FileArtifact,
    Role,
    TaskSnapshot,
)
from memnc.telegram import format_trace

from test_control import FakeBus


def _snap() -> TaskSnapshot:
    return TaskSnapshot(task_id="t1", trace_id="tr1", task="do it")


def test_debate_code_event_and_history():
    snap = _snap()
    snap.code = CoderOutput(
        summary="wrote the module",
        files=[FileArtifact(path="m.py", content="print(1)")],
    )
    bus = FakeBus()
    asyncio.run(emit_debate(bus, snap, Role.CODER))
    assert len(bus.events) == 1
    name, detail = bus.events[0]
    assert name == "debate.code"
    assert "wrote the module" in detail
    assert "m.py" in detail
    assert snap.history[-1].event == "debate.code"


def test_debate_critique_rejected_lists_issues():
    snap = _snap()
    snap.critique = CriticOutput(
        approved=False,
        summary="broken import",
        issues=[
            CriticIssue(file="m.py", line=3, severity="major", issue="import typo")
        ],
    )
    bus = FakeBus()
    asyncio.run(emit_debate(bus, snap, Role.CRITIC))
    name, detail = bus.events[0]
    assert name == "debate.critique"
    assert "REJECTED" in detail
    assert "broken import" in detail
    assert "[major] m.py:3 import typo" in detail


def test_debate_critique_approved():
    snap = _snap()
    snap.critique = CriticOutput(approved=True, summary="looks good")
    bus = FakeBus()
    asyncio.run(emit_debate(bus, snap, Role.CRITIC))
    _, detail = bus.events[0]
    assert "APPROVED" in detail


def test_debate_skips_roles_without_output():
    snap = _snap()
    bus = FakeBus()
    asyncio.run(emit_debate(bus, snap, Role.PLANNER))
    assert bus.events == []
    assert snap.history == []


def test_format_trace_renders_history():
    snap = _snap()
    snap.record("task.received", "submit")
    snap.code = CoderOutput(summary="did work")
    snap.record("debate.code", "did work\nfiles: a.py")
    out = format_trace(snap.model_dump())
    assert "#trace t1" in out
    assert "- task.received: submit" in out
    assert "- debate.code: did work files: a.py" in out


def test_format_trace_empty_history():
    out = format_trace(_snap().model_dump())
    assert "no history recorded" in out


def test_format_trace_truncates_long_entries():
    snap = _snap()
    snap.record("debate.code", "x" * 500)
    out = format_trace(snap.model_dump())
    lines = out.splitlines()
    assert all(len(l) <= 240 for l in lines)


def test_format_trace_caps_entries():
    snap = _snap()
    for i in range(40):
        snap.record("note", f"e{i}")
    out = format_trace(snap.model_dump(), max_entries=5)
    assert "(40 entries, last 5 shown)" in out
    assert "e39" in out
    assert "e35" in out
    assert "e0" not in out
