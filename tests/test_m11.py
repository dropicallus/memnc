"""M11: agents as submitters + ledger-based self-claiming (unit tests, no NATS)."""

from __future__ import annotations

import asyncio
import json

from memnc.config import Settings
from memnc.orchestrator import Orchestrator
from memnc.schema import (
    Envelope,
    Kind,
    Role,
    SUBJECT_SUBMIT,
    TaskSnapshot,
    TaskState,
    TaskSubmission,
)

# Reuse the M10 in-memory fakes (they mirror live orchestrator behaviour).
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "test_m10", Path(__file__).parent / "test_m10.py"
)
_m10 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_m10)
FakeBus = _m10.FakeBus
_FakeMsg = _m10._FakeMsg


class DispatchFakeBus(FakeBus):
    """FakeBus + publish_envelope stub (needed by the dispatch pipeline)."""

    async def publish_envelope(self, subject, envelope):
        self.events.append(("published", subject))


class ClaimNats:
    """Captures mesh.task.submit submissions, mirrors live _on_submit."""

    def __init__(self, bus: FakeBus):
        self.bus = bus
        self.submitted: list[dict] = []
        self._next_id = 2000
        # live orchestrator: depth >= max_submit_depth is rejected before a
        # snapshot exists; mirror that so tests exercise the full contract.
        self.max_submit_depth = 2

    async def request(self, subject, data, timeout=10):
        assert subject == SUBJECT_SUBMIT
        submission = json.loads(data)
        if submission.get("depth", 0) >= self.max_submit_depth:
            raise _RejectError("depth limit")
        self._next_id += 1
        task_id = f"sub-{self._next_id}"
        self.submitted.append({"task_id": task_id, **submission})
        self.bus.snapshots[task_id] = TaskSnapshot(
            task_id=task_id,
            trace_id="trace-1",
            task=submission["task"],
            context=submission.get("context", ""),
            depth=submission.get("depth", 0),
            claimable=submission.get("claimable", False),
        )
        return _FakeMsg(json.dumps({"task_id": task_id}).encode())


class _RejectError(Exception):
    pass


def _orchestrator(**settings_kwargs) -> Orchestrator:
    orchestrator = Orchestrator(Settings(**settings_kwargs))
    orchestrator.bus = DispatchFakeBus()  # type: ignore[assignment]
    return orchestrator


def _submit_message(submission: TaskSubmission):
    class _Msg:
        data = submission.model_dump_json().encode()

        async def respond(self, payload):
            self.replied = json.loads(payload)

    return _Msg()


def test_depth_zero_accepted():
    orchestrator = _orchestrator()
    msg = _submit_message(TaskSubmission(task="top task"))
    asyncio.run(orchestrator._on_submit(msg))
    assert "task_id" in msg.replied
    task_id = msg.replied["task_id"]
    snapshot = orchestrator.bus.snapshots[task_id]
    assert snapshot.depth == 0
    assert snapshot.state is TaskState.PLANNING  # dispatched normally


def test_depth_limit_rejected():
    orchestrator = _orchestrator()
    msg = _submit_message(
        TaskSubmission(task="nested", depth=Settings().max_submit_depth)
    )
    asyncio.run(orchestrator._on_submit(msg))
    assert not orchestrator.bus.snapshots  # no snapshot created
    assert "error" in msg.replied  # submitter gets an error reply


def test_fanout_subtasks_carry_depth_plus_one():
    from memnc.schema import PlannerOutput

    orchestrator = _orchestrator()
    snapshot = TaskSnapshot(
        task_id="p-deep",
        trace_id="trace-1",
        task="two parts",
        state=TaskState.PLANNING,
        fanout=True,
        depth=1,
    )
    snapshot.plan = PlannerOutput(
        plan=[
            {"step": 1, "description": "part one", "id": "s1"},
            {"step": 2, "description": "part two", "id": "s2"},
        ]
    )
    orchestrator.bus.snapshots[snapshot.task_id] = snapshot
    asyncio.run(orchestrator._fanout(snapshot))
    assert orchestrator.bus.nc.submitted  # submissions were attempted
    for sub in orchestrator.bus.nc.submitted:
        assert sub["depth"] == 2  # parent depth 1 + 1


def test_claimable_parks_in_received():
    orchestrator = _orchestrator()
    msg = _submit_message(TaskSubmission(task="self-claim me", claimable=True))
    asyncio.run(orchestrator._on_submit(msg))
    task_id = msg.replied["task_id"]
    snapshot = orchestrator.bus.snapshots[task_id]
    assert snapshot.claimable is True
    assert snapshot.state is TaskState.RECEIVED  # NOT dispatched
    assert any(e == "task.claimable" for e, _ in orchestrator.bus.events)


def test_dispatch_claimed_starts_pipeline():
    orchestrator = _orchestrator()
    snapshot = TaskSnapshot(
        task_id="claimed-1",
        trace_id="trace-1",
        task="claimed task",
        state=TaskState.RECEIVED,
        claimable=True,
        claimed_by="node-b:coder",
    )
    orchestrator.bus.snapshots[snapshot.task_id] = snapshot
    asyncio.run(orchestrator._dispatch_claimed())
    assert orchestrator.bus.snapshots["claimed-1"].state is not TaskState.RECEIVED
    history = orchestrator.bus.snapshots["claimed-1"].history
    assert any(h.event == "task.claimed" for h in history)


def test_dispatch_skips_unclaimed():
    orchestrator = _orchestrator()
    snapshot = TaskSnapshot(
        task_id="unclaimed-1",
        trace_id="trace-1",
        task="nobody wants this",
        state=TaskState.RECEIVED,
        claimable=True,
    )
    orchestrator.bus.snapshots[snapshot.task_id] = snapshot
    asyncio.run(orchestrator._dispatch_claimed())
    assert orchestrator.bus.snapshots["unclaimed-1"].state is TaskState.RECEIVED


def test_recovery_skips_unclaimed_claimable():
    """Recovery must not silently dispatch tasks an agent should claim."""
    orchestrator = _orchestrator()
    snapshot = TaskSnapshot(
        task_id="recover-1",
        trace_id="trace-1",
        task="waiting for a claimer",
        state=TaskState.RECEIVED,
        claimable=True,
    )
    orchestrator.bus.snapshots[snapshot.task_id] = snapshot
    asyncio.run(orchestrator._recover())
    assert orchestrator.bus.snapshots["recover-1"].state is TaskState.RECEIVED
    # ...but a claimed one is recovered normally
    snapshot.claimed_by = "node-a:coder"
    asyncio.run(orchestrator._recover())
    assert orchestrator.bus.snapshots["recover-1"].state is not TaskState.RECEIVED


def test_submission_depth_and_claimable_defaults():
    assert TaskSubmission(task="x").depth == 0
    assert TaskSubmission(task="x").claimable is False


def test_snapshot_claim_fields_defaults():
    snapshot = TaskSnapshot(task_id="t", trace_id="tr", task="x")
    assert snapshot.depth == 0
    assert snapshot.claimable is False
    assert snapshot.claimed_by == ""
