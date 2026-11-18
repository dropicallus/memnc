"""Tests for the 15.4.3 attempt journal (per-attempt checkpoint notes on retries)."""

import asyncio

from test_m11_claim import _snapshot
from test_m14 import SilentBus

from memnc import journal
from memnc.config import Settings
from memnc.fsm import Event
from memnc.roles import Role, build_user_prompt
from memnc.schema import CoderOutput, FileArtifact, TaskSnapshot, TaskState
from memnc.worker import Worker


def _coding_snapshot(**fields) -> TaskSnapshot:
    return _snapshot("t-j1", state=TaskState.CODING, **fields)


class _Result:
    def __init__(self, attempt_delta=1):
        self.attempt_delta = attempt_delta


def test_note_failure_appends_entry_on_retry():
    snap = _coding_snapshot()
    snap.code = CoderOutput(
        summary="wrote parser",
        files=[FileArtifact(path="parser.py", content="x")],
    )
    event = Event("result", ok=False, reason="syntax error in parser.py")
    journal.note_failure(snap, event, _Result(), Role.CODER)
    assert len(snap.attempt_journal) == 1
    entry = snap.attempt_journal[0]
    assert entry["attempt"] == snap.attempt
    assert entry["stage"] == "coder"
    assert entry["kind"] == "unknown"
    assert entry["error"] == "syntax error in parser.py"
    assert entry["files"] == ["parser.py"]
    assert entry["summary"] == "wrote parser"


def test_note_failure_ignores_success_and_non_retries():
    snap = _coding_snapshot()
    journal.note_failure(snap, Event("result", ok=True), _Result(), Role.CODER)
    journal.note_failure(snap, Event("result", ok=False, reason="x"), _Result(0), Role.CODER)
    assert snap.attempt_journal == []
    # older snapshots / fake buses without the field keep working
    snap.__dict__["attempt_journal"] = None
    journal.note_failure(snap, Event("result", ok=False, reason="x"), _Result())
    assert snap.attempt_journal is None


def test_note_failure_caps_entries_and_field_length():
    snap = _coding_snapshot()
    for i in range(12):
        snap.attempt = i
        journal.note_failure(
            snap,
            Event("result", ok=False, reason="e" * 900),
            _Result(),
            Role.CODER,
        )
    assert len(snap.attempt_journal) == journal.MAX_ENTRIES
    assert snap.attempt_journal[-1]["attempt"] == 11
    assert all(len(e["error"]) <= journal.MAX_ERROR_CHARS for e in snap.attempt_journal)


def test_note_failure_classifies_the_error():
    snap = _coding_snapshot()
    journal.note_failure(
        snap, Event("result", ok=False, reason="ReadTimeout after 180 s"), _Result(), Role.CODER
    )
    journal.note_failure(
        snap, Event("result", ok=False, reason="500 Internal Server Error"), _Result(), Role.CODER
    )
    journal.note_failure(
        snap, Event("result", ok=False, reason="no JSON object found in output"), _Result(), Role.CODER
    )
    kinds = [e["kind"] for e in snap.attempt_journal]
    assert kinds == ["timeout", "server", "invalid_output"]


def test_render_empty_and_full():
    assert journal.render(None) == ""
    assert journal.render([]) == ""
    block = journal.render(
        [
            {
                "attempt": 1,
                "stage": "coder",
                "error": "boom",
                "files": ["a.py", "b.py"],
                "summary": "half done",
            }
        ]
    )
    assert block.startswith("PROGRESS FROM PRIOR ATTEMPTS:")
    assert "attempt 1 failed at coder" in block
    assert "boom" in block and "a.py, b.py" in block and "half done" in block


def test_render_includes_failure_class_when_classified():
    assert "error (timeout): boom" in journal.render(
        [{"attempt": 1, "stage": "coder", "kind": "timeout", "error": "boom"}]
    )
    # unclassified failures add no label
    block = journal.render([{"attempt": 1, "stage": "coder", "error": "boom"}])
    assert "error: boom" in block and "(unknown)" not in block


def test_build_user_prompt_renders_journal_block():
    entries = [{"attempt": 2, "stage": "critic", "error": "verdict bad", "files": [], "summary": ""}]
    prompt = build_user_prompt(Role.CODER, {"task": "t", "attempt_journal": entries})
    assert "PROGRESS FROM PRIOR ATTEMPTS:" in prompt
    assert "attempt 2 failed at critic" in prompt
    # without entries nothing is rendered
    assert "PROGRESS FROM PRIOR ATTEMPTS:" not in build_user_prompt(Role.CODER, {"task": "t"})


def test_worker_retry_records_journal_and_redispatches():
    bus = SilentBus(Settings(role="coder", mesh_claim=True, self_pipeline=True, apply_artifacts=False))
    snap = _coding_snapshot()
    snap.code = CoderOutput(summary="partial", files=[FileArtifact(path="w.py", content="1")])
    asyncio.run(bus.put_snapshot(snap))
    w = Worker(bus.settings, bus=bus)
    asyncio.run(
        w._apply_local(
            snap,
            Event("result", ok=False, reason="tests fail"),
            role=Role.CODER,
        )
    )
    stored = asyncio.run(bus.get_snapshot("t-j1"))
    assert stored is not None and stored.attempt_journal, "retry must record a journal entry"
    entry = stored.attempt_journal[-1]
    assert entry["stage"] == "coder" and entry["error"] == "tests fail"
    assert entry["files"] == ["w.py"]
    # the retry is dispatched back to the coder
    assert stored.next_role == "coder" and stored.claimable
