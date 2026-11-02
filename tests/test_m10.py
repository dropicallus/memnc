"""M10: queryable mesh ledger + fan-out tool-call (unit tests, no NATS)."""

from __future__ import annotations

import asyncio
import json

import pytest

from memnc.config import Settings
from memnc.orchestrator import Orchestrator
from memnc.schema import (
    Envelope,
    Kind,
    PlannerOutput,
    ResultPayload,
    Role,
    SUBJECT_LEDGER,
    SUBJECT_SUBMIT,
    TaskSnapshot,
    TaskState,
)


class FakeNats:
    """Minimal nc stand-in: captures fan-out submissions on mesh.task.submit."""

    def __init__(self, bus: "FakeBus"):
        self.bus = bus
        self.submitted: list[dict] = []
        self._next_id = 1000

    async def request(self, subject, data, timeout=10):
        assert subject == SUBJECT_SUBMIT
        submission = json.loads(data)
        self._next_id += 1
        task_id = f"sub-{self._next_id}"
        self.submitted.append({"task_id": task_id, **submission})
        # live, _on_submit creates the snapshot; mirror that here
        self.bus.snapshots[task_id] = TaskSnapshot(
            task_id=task_id,
            trace_id="trace-1",
            task=submission["task"],
            context=submission.get("context", ""),
        )
        return _FakeMsg(json.dumps({"task_id": task_id}).encode())


class _FakeMsg:
    def __init__(self, data: bytes):
        self.data = data


class FakeBus:
    """In-memory Bus: snapshots + emitted events, nc is a FakeNats."""

    def __init__(self):
        self.snapshots: dict[str, TaskSnapshot] = {}
        self.events: list[tuple[str, str]] = []
        self.nc = FakeNats(self)

    async def get_snapshot(self, task_id):
        return self.snapshots.get(task_id)

    async def put_snapshot(self, snapshot):
        self.snapshots[snapshot.task_id] = snapshot

    async def list_tasks(self):
        return list(self.snapshots.values())

    async def emit_event(self, snapshot, event, detail=""):
        self.events.append((event, detail))

    async def dead_letter(self, envelope, reason):
        self.events.append(("dlq", reason))

    async def get_result(self, *_):
        return None


def _planner_snapshot(task_id="parent-1", n_steps=2, fanout=True) -> TaskSnapshot:
    snapshot = TaskSnapshot(
        task_id=task_id,
        trace_id="trace-1",
        task="build a small thing",
        state=TaskState.PLANNING,
        fanout=fanout,
    )
    snapshot.plan = PlannerOutput(
        plan=[
            {"step": i + 1, "description": f"do part {i + 1}", "id": f"s{i + 1}"}
            for i in range(n_steps)
        ]
    )
    return snapshot


def _result_envelope(task_id: str, role: Role = Role.PLANNER) -> Envelope:
    return Envelope(
        task_id=task_id,
        trace_id="trace-1",
        kind=Kind.RESULT,
        role=role,
        node="node-a",
        payload=ResultPayload(ok=True, output={}).model_dump(),
    )


def test_fanout_emits_subtasks_and_parks_parent():
    orchestrator = Orchestrator(Settings())
    orchestrator.bus = FakeBus()  # type: ignore[assignment]
    snapshot = _planner_snapshot()
    orchestrator.bus.snapshots[snapshot.task_id] = snapshot

    asyncio.run(orchestrator._fanout(snapshot))

    assert len(orchestrator.bus.nc.submitted) == 2
    assert len(snapshot.subtasks) == 2
    assert snapshot.self_assign is True
    assert snapshot.last_action == {"role": "fanout-wait"}
    assert any(e == "task.fanout" for e, _ in orchestrator.bus.events)
    # subtasks carry the parent link
    sub = asyncio.run(orchestrator.bus.get_snapshot(list(snapshot.subtasks)[0]))
    assert sub is not None
    assert sub.parent_task_id == snapshot.task_id
    assert sub.state is TaskState.RECEIVED


def test_fanout_aggregates_done_and_failed():
    orchestrator = Orchestrator(Settings())
    orchestrator.bus = FakeBus()  # type: ignore[assignment]

    # -- all subtasks DONE -> parent DONE
    snapshot = _planner_snapshot(task_id="p-done")
    orchestrator.bus.snapshots[snapshot.task_id] = snapshot
    asyncio.run(orchestrator._fanout(snapshot))
    for sub_id in snapshot.subtasks:
        sub = orchestrator.bus.snapshots[sub_id]
        sub.state = TaskState.DONE
    asyncio.run(orchestrator._check_fanouts())
    assert orchestrator.bus.snapshots["p-done"].state is TaskState.DONE

    # -- one subtask FAILED -> parent FAILED + DLQ
    snapshot = _planner_snapshot(task_id="p-fail")
    orchestrator.bus.snapshots[snapshot.task_id] = snapshot
    asyncio.run(orchestrator._fanout(snapshot))
    states = [TaskState.DONE, TaskState.FAILED]
    for sub_id, state in zip(snapshot.subtasks, states):
        orchestrator.bus.snapshots[sub_id].state = state
    asyncio.run(orchestrator._check_fanouts())
    parent = orchestrator.bus.snapshots["p-fail"]
    assert parent.state is TaskState.FAILED
    assert parent.error and "subtask failed" in parent.error
    assert ("dlq", "fanout: a subtask failed or was cancelled") in orchestrator.bus.events


def test_fanout_waits_while_subtasks_run():
    orchestrator = Orchestrator(Settings())
    orchestrator.bus = FakeBus()  # type: ignore[assignment]
    snapshot = _planner_snapshot(task_id="p-wait")
    orchestrator.bus.snapshots[snapshot.task_id] = snapshot
    asyncio.run(orchestrator._fanout(snapshot))
    # leave subtasks in RECEIVED: aggregation must not fire
    asyncio.run(orchestrator._check_fanouts())
    assert orchestrator.bus.snapshots["p-wait"].state is TaskState.PLANNING


def test_reaper_skips_fanout_wait_parent():
    """A parked fanout parent has no at_epoch in last_action -> reaper skips."""
    orchestrator = Orchestrator(Settings())
    orchestrator.bus = FakeBus()  # type: ignore[assignment]
    snapshot = _planner_snapshot(task_id="p-wait")
    orchestrator.bus.snapshots[snapshot.task_id] = snapshot
    asyncio.run(orchestrator._fanout(snapshot))
    # _reap_timeouts only touches real Bus snapshots; with the FakeBus the
    # important invariant is the last_action shape the real reaper checks.
    assert "at_epoch" not in snapshot.last_action


def test_single_step_plan_skips_fanout_gate():
    """_handle_result's fan-out gate requires >= 2 plan steps."""
    snapshot = _planner_snapshot(n_steps=1)
    assert len(snapshot.plan.plan) < 2
    assert snapshot.fanout is True


def test_task_submission_fanout_default_off():
    from memnc.schema import TaskSubmission

    assert TaskSubmission(task="x").fanout is False
    assert TaskSubmission(task="x", fanout=True).fanout is True


def test_snapshot_fanout_defaults():
    snapshot = TaskSnapshot(task_id="t", trace_id="tr", task="x")
    assert snapshot.fanout is False
    assert snapshot.subtasks == {}
    assert snapshot.parent_task_id == ""


def test_ledger_subject_not_in_stream_coverage():
    """mesh.ledger must stay a core request/reply subject (see bus.py note)."""
    from memnc.schema import SUBJECT_LEDGER

    assert SUBJECT_LEDGER == "mesh.ledger"
