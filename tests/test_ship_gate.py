"""Ship-gate V2: mechanical deliverable checks (docs/design-ship-gate.md).

The orchestrator must refuse coder results whose claimed deliverables are
missing, thin stubs, or too few - based on worker-stamped evidence
(artifact_sizes / artifact_lines), never on the coder narrative.
"""

import asyncio
import json
import os

from test_done_gate import _coding_snapshot, _stub_publish
from test_m10 import FakeBus, _FakeMsg

from memnc.config import Settings
from memnc.fsutil import artifact_line_counts
from memnc.orchestrator import Orchestrator
from memnc.schema import (
    Envelope,
    Kind,
    ResultPayload,
    Role,
    TaskState,
)


def _coder_envelope(
    task_id: str,
    artifact_sizes: dict[str, int],
    artifact_lines: dict[str, int] | None = None,
) -> Envelope:
    payload = ResultPayload(
        ok=True,
        output={"summary": "wrote it"},
        artifact_sizes=artifact_sizes,
        artifact_lines=artifact_lines or {},
    )
    return Envelope(
        task_id=task_id,
        trace_id="tr-1",
        kind=Kind.RESULT,
        role=Role.CODER,
        node="node-a",
        payload=payload.model_dump(),
    )


def _run_gate(
    tmp_path,
    snap,
    envelope: Envelope,
    **settings,
) -> Orchestrator:
    bus = FakeBus()
    _stub_publish(bus)
    orch = Orchestrator(
        settings=Settings(data_dir=str(tmp_path), **settings), bus=bus
    )
    bus.snapshots[snap.task_id] = snap
    msg = _FakeMsg(json.dumps(envelope.model_dump()).encode())
    asyncio.run(orch._handle_result(msg))
    return orch


def test_artifact_line_counts_non_blank_lines(tmp_path) -> None:
    (tmp_path / "main.py").write_text("def f():\n    return 1\n\n\n# end\n")
    counts = artifact_line_counts(tmp_path, [{"path": "main.py", "content": ""}])
    assert counts == {"main.py": 3}


def test_artifact_line_counts_missing_and_unsafe(tmp_path) -> None:
    counts = artifact_line_counts(
        tmp_path, [{"path": "ghost.md", "content": ""}, {"path": "../escape", "content": ""}]
    )
    assert counts["ghost.md"] == -1
    assert counts["../escape"] == -1


def test_gate_refuses_thin_stub(tmp_path) -> None:
    snap = _coding_snapshot("t-ship-1", attempt=2)
    _run_gate(
        tmp_path,
        snap,
        _coder_envelope(
            "t-ship-1",
            {"fsm/worker.py": 412},
            artifact_lines={"fsm/worker.py": 5},
        ),
        verify_min_lines=20,
    )
    assert snap.state is TaskState.FAILED
    assert "verify_min_lines" in (snap.error or "")
    assert "fsm/worker.py (5 lines < 20)" in (snap.error or "")


def test_gate_accepts_substantial_file(tmp_path) -> None:
    snap = _coding_snapshot("t-ship-2")
    _run_gate(
        tmp_path,
        snap,
        _coder_envelope(
            "t-ship-2",
            {"fsm/worker.py": 1200},
            artifact_lines={"fsm/worker.py": 42},
        ),
        verify_min_lines=20,
    )
    assert snap.state is not TaskState.FAILED


def test_gate_still_refuses_missing_files(tmp_path) -> None:
    snap = _coding_snapshot("t-ship-3", attempt=2)
    _run_gate(
        tmp_path,
        snap,
        _coder_envelope("t-ship-3", {"ghost.md": -1}, artifact_lines={"ghost.md": -1}),
        verify_min_lines=20,
    )
    assert snap.state is TaskState.FAILED
    assert "claimed artifacts missing from workspace: ghost.md" in (snap.error or "")


def test_gate_refuses_too_few_files(tmp_path) -> None:
    snap = _coding_snapshot("t-ship-4", attempt=2)
    _run_gate(
        tmp_path,
        snap,
        _coder_envelope("t-ship-4", {"one.py": 900}, artifact_lines={"one.py": 30}),
        verify_min_files=2,
    )
    assert snap.state is TaskState.FAILED
    assert "verify_min_files=2" in (snap.error or "")


def test_gate_off_by_default_allows_thin(tmp_path) -> None:
    snap = _coding_snapshot("t-ship-5")
    _run_gate(
        tmp_path,
        snap,
        _coder_envelope("t-ship-5", {"notes.md": 30}, artifact_lines={"notes.md": 2}),
    )
    assert snap.state is not TaskState.FAILED


def test_gate_ignores_critic_results(tmp_path) -> None:
    snap = _coding_snapshot("t-ship-6")
    payload = ResultPayload(ok=True, output={"summary": "ok", "approved": True})
    env = Envelope(
        task_id=snap.task_id,
        trace_id="tr-1",
        kind=Kind.RESULT,
        role=Role.CRITIC,
        node="node-a",
        payload=payload.model_dump(),
    )
    _run_gate(tmp_path, snap, env, verify_min_lines=20, verify_min_files=2)
    assert snap.state is not TaskState.FAILED


def test_gate_treats_old_worker_without_lines_as_thin(tmp_path) -> None:
    """A worker that does not stamp artifact_lines yet: claimed files count
    as 0 lines when the gate is on, so an old worker cannot smuggle stubs."""
    snap = _coding_snapshot("t-ship-7", attempt=2)
    _run_gate(
        tmp_path,
        snap,
        _coder_envelope("t-ship-7", {"fsm/worker.py": 412}, artifact_lines={}),
        verify_min_lines=20,
    )
    assert snap.state is TaskState.FAILED
    assert "fsm/worker.py (0 lines < 20)" in (snap.error or "")


def test_settings_env_roundtrip() -> None:
    os.environ["VERIFY_MIN_LINES"] = "20"
    os.environ["VERIFY_MIN_FILES"] = "1"
    try:
        s = Settings(data_dir="/tmp/x")
        assert s.verify_min_lines == 20
        assert s.verify_min_files == 1
    finally:
        del os.environ["VERIFY_MIN_LINES"]
        del os.environ["VERIFY_MIN_FILES"]