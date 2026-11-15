"""15.4.5 DONE-gate hardening: structural activity gate (design note in roadmap)."""

import asyncio
import json

from test_m10 import FakeBus, _FakeMsg
from test_worker import FakeTools

from memnc.config import Settings
from memnc.fsm import done_block_reason
from memnc.fsutil import verify_artifacts
from memnc.orchestrator import Orchestrator
from memnc.schema import (
    CriticOutput,
    Envelope,
    FileArtifact,
    Kind,
    ResultPayload,
    Role,
    TaskSnapshot,
    TaskState,
)
from memnc.worker import Worker


def _snapshot(task_id: str = "t-done-1") -> TaskSnapshot:
    return TaskSnapshot(task_id=task_id, trace_id="tr-done-1", task="write a report")


def test_done_block_reason_blocks_empty() -> None:
    snap = _snapshot()
    assert done_block_reason(snap) == "structurally rejected: no executed commands or tool calls"


def test_done_block_reason_allows_tool_call() -> None:
    snap = _snapshot()
    snap.completed_tool_calls.append("fs.write")
    assert done_block_reason(snap) == ""


def test_done_block_reason_allows_command() -> None:
    snap = _snapshot()
    snap.completed_commands.append("abc-123")
    assert done_block_reason(snap) == ""


def _critique_snapshot(task_id: str, approved: bool = True) -> TaskSnapshot:
    snap = TaskSnapshot(task_id=task_id, trace_id="tr-1", task="write a report")
    snap.state = TaskState.CRITIQUE
    snap.critique = CriticOutput(approved=approved, summary="ok")
    return snap


def _critic_envelope(task_id: str) -> Envelope:
    return Envelope(
        task_id=task_id,
        trace_id="tr-1",
        kind=Kind.RESULT,
        role=Role.CRITIC,
        node="node-a",
        payload=ResultPayload(ok=True, output={"summary": "ok", "approved": True}).model_dump(),
    )


def test_orchestrator_gate_blocks_done_without_activity(tmp_path) -> None:
    bus = FakeBus()
    orch = Orchestrator(
        settings=Settings(done_requires_activity=True, data_dir=str(tmp_path)), bus=bus
    )
    snap = _critique_snapshot("t-gate-1")
    bus.snapshots[snap.task_id] = snap
    msg = _FakeMsg(json.dumps(_critic_envelope(snap.task_id).model_dump()).encode())

    asyncio.run(orch._handle_result(msg))

    assert snap.state is TaskState.FAILED
    assert "no executed commands" in (snap.error or "")
    events = [e.event for e in snap.history]
    assert "task.failed" in events and "task.done" not in events


def test_orchestrator_gate_off_allows_done(tmp_path) -> None:
    bus = FakeBus()
    orch = Orchestrator(settings=Settings(data_dir=str(tmp_path)), bus=bus)
    snap = _critique_snapshot("t-gate-2")
    bus.snapshots[snap.task_id] = snap
    msg = _FakeMsg(json.dumps(_critic_envelope(snap.task_id).model_dump()).encode())

    asyncio.run(orch._handle_result(msg))

    assert snap.state is TaskState.DONE
    assert "task.done" in [e.event for e in snap.history]


def _stub_publish(bus: FakeBus) -> None:
    async def _publish(*args, **kwargs):
        return None

    bus.publish_envelope = _publish  # type: ignore[method-assign]


def _worker(settings: Settings, bus: FakeBus) -> Worker:
    return Worker(settings, backend=None, tools=FakeTools(), bus=bus)


def test_worker_gate_blocks_done_without_activity(tmp_path) -> None:
    bus = FakeBus()
    w = _worker(
        Settings(role="coder", workspace_root=tmp_path, mcp_enabled=False,
                 done_requires_activity=True),
        bus,
    )
    snap = _critique_snapshot("t-gate-3")
    bus.snapshots[snap.task_id] = snap

    asyncio.run(w._apply_local(snap, _event()))

    assert snap.state is TaskState.FAILED
    assert "no executed commands" in (snap.error or "")


def test_worker_gate_off_allows_done(tmp_path) -> None:
    bus = FakeBus()
    w = _worker(Settings(role="coder", workspace_root=tmp_path, mcp_enabled=False), bus)
    snap = _critique_snapshot("t-gate-4")
    bus.snapshots[snap.task_id] = snap

    asyncio.run(w._apply_local(snap, _event()))

    assert snap.state is TaskState.DONE


def _event():
    from memnc.fsm import Event

    return Event("result", ok=True, approved=True)


# ---- 15.4.5 gap 3: claimed-vs-existing artifact cross-check ----


def test_verify_artifacts_maps_sizes_and_missing(tmp_path) -> None:
    (tmp_path / "real.txt").write_text("hello")
    sizes = verify_artifacts(
        tmp_path, [FileArtifact(path="real.txt", content="x"), FileArtifact(path="ghost.txt", content="x")]
    )
    assert sizes["real.txt"] == 5
    assert sizes["ghost.txt"] == -1


def test_verify_artifacts_unsafe_path_is_missing(tmp_path) -> None:
    sizes = verify_artifacts(tmp_path, [FileArtifact(path="../escape.txt", content="x")])
    assert sizes == {"../escape.txt": -1}


def _coding_snapshot(task_id: str, attempt: int = 0) -> TaskSnapshot:
    snap = TaskSnapshot(task_id=task_id, trace_id="tr-1", task="code it")
    snap.state = TaskState.CODING
    snap.attempt = attempt
    return snap


def _coder_envelope(task_id: str, artifact_sizes: dict[str, int]) -> Envelope:
    return Envelope(
        task_id=task_id,
        trace_id="tr-1",
        kind=Kind.RESULT,
        role=Role.CODER,
        node="node-a",
        payload=ResultPayload(ok=True, output={"summary": "wrote it"}, artifact_sizes=artifact_sizes).model_dump(),
    )


def test_orchestrator_fails_on_missing_claimed_artifact(tmp_path) -> None:
    bus = FakeBus()
    orch = Orchestrator(settings=Settings(data_dir=str(tmp_path)), bus=bus)
    snap = _coding_snapshot("t-art-1", attempt=2)
    bus.snapshots[snap.task_id] = snap
    msg = _FakeMsg(json.dumps(_coder_envelope(snap.task_id, {"ghost.md": -1}).model_dump()).encode())

    asyncio.run(orch._handle_result(msg))

    assert snap.state is TaskState.FAILED
    assert "claimed artifacts missing from workspace: ghost.md" in (snap.error or "")


def test_orchestrator_accepts_existing_claimed_artifacts(tmp_path) -> None:
    bus = FakeBus()
    orch = Orchestrator(settings=Settings(data_dir=str(tmp_path)), bus=bus)
    snap = _coding_snapshot("t-art-2")
    bus.snapshots[snap.task_id] = snap
    _stub_publish(bus)
    msg = _FakeMsg(json.dumps(_coder_envelope(snap.task_id, {"main.py": 120}).model_dump()).encode())

    asyncio.run(orch._handle_result(msg))

    assert snap.state is not TaskState.FAILED
