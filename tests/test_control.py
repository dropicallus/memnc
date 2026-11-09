"""T2: human intervention via mesh.control — grant gate, submit/cancel/retry/stop."""

from __future__ import annotations

import asyncio
import json

from memnc.config import Settings
from memnc.orchestrator import Orchestrator
from memnc.schema import (
    Capability,
    ControlCommand,
    GrantLevel,
    GrantRecord,
    TaskSnapshot,
    TaskState,
)


class _FakeMsg:
    def __init__(self, data: bytes):
        self.data = data
        self.replied: bytes | None = None

    async def respond(self, data: bytes) -> None:
        self.replied = data


class FakeBus:
    def __init__(self, grants: dict[str, GrantRecord] | None = None):
        self.snapshots: dict[str, TaskSnapshot] = {}
        self.events: list[tuple[str, str]] = []
        self.grants = grants or {}
        self.capabilities: list[Capability] = []
        self.withdrawn: list[str] = []
        self.published: list = []
        self.nc = None  # not used in unit tests

    async def grant_read(self, agent_id):
        return self.grants.get(agent_id)

    async def get_snapshot(self, task_id):
        return self.snapshots.get(task_id)

    async def put_snapshot(self, snapshot):
        self.snapshots[snapshot.task_id] = snapshot

    async def emit_event(self, snapshot, event, detail=""):
        self.events.append((event, detail))

    async def publish_envelope(self, subject, envelope):
        self.published.append((subject, envelope))

    async def list_capabilities(self):
        return list(self.capabilities)

    async def withdraw_capability(self, agent_id, skill=None):
        self.withdrawn.append(agent_id)

    async def list_nodes(self):
        return []

    async def list_model_requests(self):
        return []


_GRANTED = GrantRecord(
    agent_id="telegram-123", level=GrantLevel.CHAIN,
    granted_by="root",
)


def _orch(grants=None) -> Orchestrator:
    orch = Orchestrator(Settings())
    orch.bus = FakeBus(grants)  # type: ignore[assignment]
    return orch


def _cmd(**kw) -> ControlCommand:
    base = dict(action="submit", agent_id="telegram-123", task="write hello")
    base.update(kw)
    return ControlCommand(**base)


def _run(orch, cmd) -> dict:
    msg = _FakeMsg(cmd.model_dump_json().encode())
    asyncio.run(orch._on_control(msg))
    assert msg.replied is not None
    return json.loads(msg.replied)


def test_denied_without_grant():
    orch = _orch()
    resp = _run(orch, _cmd())
    assert "error" in resp
    assert not orch.bus.snapshots


def test_submit_creates_task_and_records_human_event():
    orch = _orch({"telegram-123": _GRANTED})
    resp = _run(orch, _cmd())
    assert resp["ok"] is True
    snapshot = orch.bus.snapshots[resp["task_id"]]
    assert snapshot.state is TaskState.PLANNING  # dispatched like meshctl submit
    assert any(h.event == "human.submit" for h in snapshot.history)


def test_submit_requires_task_text():
    orch = _orch({"telegram-123": _GRANTED})
    resp = _run(orch, _cmd(task="   "))
    assert "error" in resp


def test_cancel_moves_task_to_cancelled():
    orch = _orch({"telegram-123": _GRANTED})
    resp = _run(orch, _cmd())
    resp2 = _run(orch, _cmd(action="cancel", task_id=resp["task_id"], task=""))
    assert resp2["ok"] is True
    assert orch.bus.snapshots[resp["task_id"]].state is TaskState.CANCELLED
    snapshot = orch.bus.snapshots[resp["task_id"]]
    assert any(h.event == "human.cancel" for h in snapshot.history)


def test_retry_redispatches_failed_task():
    orch = _orch({"telegram-123": _GRANTED})
    resp = _run(orch, _cmd())
    task_id = resp["task_id"]
    orch.bus.snapshots[task_id].state = TaskState.FAILED
    resp2 = _run(orch, _cmd(action="retry", task_id=task_id, task=""))
    assert resp2["ok"] is True
    assert orch.bus.snapshots[task_id].state is TaskState.PLANNING
    snapshot = orch.bus.snapshots[task_id]
    assert any(h.event == "human.retry" for h in snapshot.history)


def test_retry_rejects_terminal_done_task():
    orch = _orch({"telegram-123": _GRANTED})
    resp = _run(orch, _cmd())
    task_id = resp["task_id"]
    orch.bus.snapshots[task_id].state = TaskState.DONE
    resp2 = _run(orch, _cmd(action="retry", task_id=task_id, task=""))
    assert "error" in resp2


def test_cancel_unknown_task_errors():
    orch = _orch({"telegram-123": _GRANTED})
    resp = _run(orch, _cmd(action="cancel", task_id="nope", task=""))
    assert "error" in resp


def test_stop_worker_withdraws_node_capabilities():
    orch = _orch({"telegram-123": _GRANTED})
    orch.bus.capabilities = [
        Capability(agent_id="n1-coder", skill="code", node="n1"),
        Capability(agent_id="n1-critic", skill="critic", node="n1"),
        Capability(agent_id="n2-coder", skill="code", node="n2"),
    ]
    resp = _run(orch, _cmd(action="stop_worker", task="", node="n1"))
    assert resp["ok"] is True
    assert sorted(orch.bus.withdrawn) == ["n1-coder", "n1-critic"]


def test_stop_worker_requires_node():
    orch = _orch({"telegram-123": _GRANTED})
    resp = _run(orch, _cmd(action="stop_worker", task="", node="  "))
    assert "error" in resp
