"""T4: human pause/resume of nodes via mesh.control."""

from __future__ import annotations

import asyncio

from test_control import _GRANTED, FakeBus, _FakeMsg
from test_control import _orch as _control_orch

from memnc.schema import ControlCommand, NodeInfo
from memnc.worker import Worker


class _NodeBus(FakeBus):
    """FakeBus plus an in-memory roster for pause tests."""

    def __init__(self, grants=None):
        super().__init__(grants)
        self.nodes: dict[str, NodeInfo] = {}

    async def get_node(self, node_id):
        return self.nodes.get(node_id)

    async def set_node_paused(self, node_id, paused):
        info = self.nodes.get(node_id)
        if info is None:
            return None
        info.paused = paused
        return info


def _cmd(**kw) -> ControlCommand:
    base = dict(action="pause_node", agent_id="telegram-123", task="")
    base.update(kw)
    return ControlCommand(**base)


def _run(orch, cmd) -> dict:
    import json

    msg = _FakeMsg(cmd.model_dump_json().encode())
    asyncio.run(orch._on_control(msg))
    assert msg.replied is not None
    return json.loads(msg.replied)


def test_pause_and_resume_node():
    orch = _control_orch({"telegram-123": _GRANTED})
    orch.bus = _NodeBus({"telegram-123": _GRANTED})
    orch.bus.nodes["n1"] = NodeInfo(node_id="n1", roles=["coder"], updated_at="t")
    resp = _run(orch, _cmd(node="n1"))
    assert resp == {"ok": True, "node": "n1", "paused": True}
    assert orch.bus.nodes["n1"].paused is True
    resp2 = _run(orch, _cmd(action="resume_node", node="n1"))
    assert resp2 == {"ok": True, "node": "n1", "paused": False}
    assert orch.bus.nodes["n1"].paused is False


def test_pause_unknown_node_errors():
    orch = _control_orch({"telegram-123": _GRANTED})
    orch.bus = _NodeBus({"telegram-123": _GRANTED})
    resp = _run(orch, _cmd(node="ghost"))
    assert "error" in resp


def test_pause_requires_node():
    orch = _control_orch({"telegram-123": _GRANTED})
    resp = _run(orch, _cmd(node="  "))
    assert "error" in resp


def test_pause_requires_grant():
    orch = _control_orch()
    resp = _run(orch, _cmd(node="n1"))
    assert "error" in resp


def test_nodeinfo_merge_keeps_pause_through_heartbeat():
    paused = NodeInfo(node_id="n1", roles=["coder"], paused=True, updated_at="old")
    fresh = NodeInfo(node_id="n1", roles=["coder"], updated_at="new")
    merged = paused.merge(fresh)
    assert merged.paused is True


def test_worker_pause_check():
    bus = _NodeBus()
    bus.nodes["n1"] = NodeInfo(node_id="n1", roles=["coder"], paused=True, updated_at="t")
    import tempfile

    from memnc.config import Settings

    with tempfile.TemporaryDirectory() as tmp:
        worker = Worker(
            Settings(role="coder", workspace_root=tmp, node_name="n1", mcp_enabled=False),
            backend=None,
            tools=type("T", (), {"available": True, "schemas": [], "call": None})(),
            bus=bus,
        )
        assert asyncio.run(worker._paused()) is True
        bus.nodes["n1"].paused = False
        assert asyncio.run(worker._paused()) is False
        assert asyncio.run(worker._paused()) is False  # unknown node → not paused
        bus.nodes.clear()
        assert asyncio.run(worker._paused()) is False
