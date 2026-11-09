"""T3: approval gate — FSM parking + human decide via mesh.control."""

from __future__ import annotations

import asyncio
import json

from memnc.config import Settings
from memnc.fsm import Event, transition
from memnc.orchestrator import Orchestrator
from memnc.schema import GrantLevel, GrantRecord, TaskState

from test_control import FakeBus, _FakeMsg

_GRANTED = GrantRecord(
    agent_id="telegram-123", level=GrantLevel.CHAIN, granted_by="root"
)


def _orch() -> Orchestrator:
    orch = Orchestrator(Settings())
    orch.bus = FakeBus({"telegram-123": _GRANTED})  # type: ignore[assignment]
    return orch


def _run(orch, **kw) -> dict:
    base = dict(action="decide", agent_id="telegram-123", task_id="t1")
    base.update(kw)
    msg = _FakeMsg(json.dumps(base).encode())
    asyncio.run(orch._on_control(msg))
    assert msg.replied is not None
    return json.loads(msg.replied)


def _critique_event(approved: bool) -> Event:
    return Event("result", ok=True, approved=approved)


# --- FSM -------------------------------------------------------------------

def test_gate_never_approval_finishes():
    t = transition(TaskState.CRITIQUE, _critique_event(True), gate="never")
    assert t.state is TaskState.DONE


def test_gate_always_parks_even_on_approval():
    t = transition(TaskState.CRITIQUE, _critique_event(True), gate="always")
    assert t.state is TaskState.CRITIQUE
    assert any(a.type == "park" for a in t.actions)


def test_gate_fail_parks_on_rejection():
    t = transition(TaskState.CRITIQUE, _critique_event(False), gate="fail")
    assert t.state is TaskState.CRITIQUE
    assert any(a.type == "park" for a in t.actions)


def test_gate_fail_auto_revises_without_park():
    # gate=fail with an approval must NOT park — plain finish path.
    t = transition(TaskState.CRITIQUE, _critique_event(True), gate="fail")
    assert t.state is TaskState.DONE


def test_human_decision_approve_finishes():
    t = transition(TaskState.CRITIQUE, Event("human_decision", approved=True))
    assert t.state is TaskState.DONE


def test_human_decision_reject_revises():
    t = transition(
        TaskState.CRITIQUE,
        Event("human_decision", approved=False),
        revisions=0,
        max_revisions=3,
    )
    assert t.state is TaskState.REVISE
    assert t.revision_delta == 1
    assert t.attempt_reset is True


def test_human_decision_reject_exhausted_fails():
    t = transition(
        TaskState.CRITIQUE,
        Event("human_decision", approved=False),
        revisions=3,
        max_revisions=3,
    )
    assert t.state is TaskState.FAILED


# --- Orchestrator decide ---------------------------------------------------

def test_decide_approve_finishes_parked_task():
    from memnc.schema import TaskSnapshot

    orch = _orch()
    snap = TaskSnapshot(task_id="t1", trace_id="r1", task="x", gate="always")
    snap.state = TaskState.CRITIQUE
    snap.needs_human = True
    orch.bus.snapshots["t1"] = snap
    resp = _run(orch, approved=True)
    assert resp["ok"] is True and resp["verdict"] == "approved"
    assert snap.state is TaskState.DONE
    assert snap.needs_human is False
    assert any(e == "human.decision" for e, _ in orch.bus.events)


def test_decide_reject_revises_parked_task():
    from memnc.schema import TaskSnapshot

    orch = _orch()
    snap = TaskSnapshot(task_id="t1", trace_id="r1", task="x", gate="always")
    snap.state = TaskState.CRITIQUE
    snap.needs_human = True
    orch.bus.snapshots["t1"] = snap
    resp = _run(orch, approved=False)
    assert resp["ok"] is True and resp["verdict"] == "rejected"
    # reject -> revision immediately redispatched to the coder
    assert snap.state is TaskState.CODING
    assert snap.revisions == 1


def test_decide_requires_parked_task():
    from memnc.schema import TaskSnapshot

    orch = _orch()
    snap = TaskSnapshot(task_id="t1", trace_id="r1", task="x")
    snap.state = TaskState.PLANNING
    orch.bus.snapshots["t1"] = snap
    resp = _run(orch, approved=True)
    assert "error" in resp


def test_decide_requires_approved_flag():
    from memnc.schema import TaskSnapshot

    orch = _orch()
    snap = TaskSnapshot(task_id="t1", trace_id="r1", task="x", gate="always")
    snap.state = TaskState.CRITIQUE
    snap.needs_human = True
    orch.bus.snapshots["t1"] = snap
    resp = _run(orch)
    assert "error" in resp


def test_submit_with_gate_is_carried():
    orch = _orch()
    msg = _FakeMsg(
        json.dumps(
            {"action": "submit", "agent_id": "telegram-123", "task": "hi",
             "gate": "always"}
        ).encode()
    )
    asyncio.run(orch._on_control(msg))
    snap = next(iter(orch.bus.snapshots.values()))
    assert snap.gate == "always"
