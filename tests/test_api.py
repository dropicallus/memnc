"""Tests for the M11a/M11b/M11c mesh API server (src/memnc/api.py)."""

from __future__ import annotations

import asyncio
import inspect
import json

import pytest

from memnc.api import WRITE_VERBS, ApiServer
from memnc.schema import (
    SUBJECT_CONTROL,
    Capability,
    ControlCommand,
    GrantLevel,
    GrantRecord,
    NodeInfo,
    TaskSnapshot,
    TaskState,
    TrustEntry,
)


def _snap(
    task_id: str,
    state: TaskState = TaskState.RECEIVED,
    claimed_by: str = "",
    task: str = "Write docs/x.md:\nbody",
) -> TaskSnapshot:
    return TaskSnapshot(
        task_id=task_id,
        trace_id=task_id,
        task=task,
        state=state,
        claimed_by=claimed_by,
    )


class FakeBus:
    def __init__(self) -> None:
        self.snaps = [
            _snap("t1", TaskState.CODING, claimed_by="node-a-coder:coder"),
            _snap("t2", TaskState.DONE, claimed_by="node-a-coder:coder"),
            _snap("t3", TaskState.RECEIVED),
        ]
        self.nodes = [
            NodeInfo(node_id="node-a", roles=["coder"], models=["m1"]),
            NodeInfo(node_id="node-b", roles=["planner"], models=["m2"]),
        ]

    async def list_tasks(self) -> list[TaskSnapshot]:
        return self.snaps

    async def get_snapshot(self, task_id: str) -> TaskSnapshot | None:
        return next((s for s in self.snaps if s.task_id == task_id), None)

    async def list_nodes(self) -> list[NodeInfo]:
        return self.nodes

    async def get_node(self, node_id: str) -> NodeInfo | None:
        return next((n for n in self.nodes if n.node_id == node_id), None)


class FakeMsg:
    def __init__(self, data: bytes, reply: str = "inbox.test"):
        self.data = data
        self.reply = reply


@pytest.fixture()
def server() -> ApiServer:
    from memnc.config import Settings

    s = Settings(node_name="test-node")
    return ApiServer(s, FakeBus())  # type: ignore[arg-type]


def test_task_list_filters_by_state(server: ApiServer):
    import asyncio

    out = asyncio.run(server.task_list(state="DONE"))
    assert len(out) == 1 and out[0]["task_id"] == "t2"


def test_task_list_filters_by_node(server: ApiServer):
    import asyncio

    out = asyncio.run(server.task_list(node="node-a"))
    assert len(out) == 2
    out = asyncio.run(server.task_list(node="node-b"))
    assert out == []


def test_dispatch_unknown_verb(server: ApiServer):
    import asyncio

    with pytest.raises(ValueError):
        asyncio.run(server.dispatch("nope", {}))


# -- M11b: write verbs forward to the orchestrator control gate ---------------


class FakeNcControl:
    """Captures the mesh.control request and replies like the orchestrator."""

    def __init__(self, reply: dict) -> None:
        self.reply = reply
        self.subjects: list[str] = []
        self.commands: list[ControlCommand] = []

    async def request(self, subject: str, payload: bytes, timeout: float = 5):
        self.subjects.append(subject)
        self.commands.append(ControlCommand.model_validate_json(payload))
        return FakeMsg(json.dumps(self.reply).encode())


def _write_server(reply: dict) -> tuple[ApiServer, FakeNcControl]:
    from memnc.config import Settings

    nc = FakeNcControl(reply)
    s = Settings(node_name="test-node")
    bus = FakeBus()
    bus.nc = nc  # type: ignore[assignment]
    return ApiServer(s, bus), nc  # type: ignore[arg-type]


def test_write_verbs_cover_t2_actions():
    expected = {
        "submit",
        "cancel",
        "retry",
        "decide",
        "stop_worker",
        "pause_node",
        "resume_node",
    }
    assert {v.split(".", 1)[1] for v in WRITE_VERBS} == expected


def test_write_verb_forwards_control_command():
    import asyncio

    server, nc = _write_server({"ok": True, "task_id": "t9"})
    out = asyncio.run(
        server.dispatch("task.submit", {"agent_id": "telegram-42", "task": "audit the mesh"})
    )
    assert nc.subjects == [SUBJECT_CONTROL]
    cmd = nc.commands[0]
    assert cmd.action == "submit" and cmd.agent_id == "telegram-42"
    assert cmd.task == "audit the mesh"
    assert out["task_id"] == "t9"


def test_write_verb_requires_agent_id(server: ApiServer):
    import asyncio

    with pytest.raises(ValueError, match="agent_id"):
        asyncio.run(server.dispatch("task.cancel", {"task_id": "t1"}))


def test_write_verb_maps_verb_to_action():
    import asyncio

    server, nc = _write_server({"ok": True})
    for verb, action in (
        ("task.retry", "retry"),
        ("task.decide", "decide"),
        ("node.stop_worker", "stop_worker"),
        ("node.pause_node", "pause_node"),
        ("node.resume_node", "resume_node"),
    ):
        asyncio.run(server.dispatch(verb, {"agent_id": "tg", "task_id": "t", "node": "n"}))
        assert nc.commands[-1].action == action


def test_write_verb_denied_reply_raises():
    import asyncio

    server, _nc = _write_server({"error": "not granted"})
    with pytest.raises(PermissionError, match="not granted"):
        asyncio.run(server.dispatch("task.cancel", {"agent_id": "tg", "task_id": "t1"}))


def test_dispatch_node_detail_includes_tasks(server: ApiServer):
    import asyncio

    data = asyncio.run(server.dispatch("node.detail", {"node_id": "node-a"}))
    assert data["node"]["node_id"] == "node-a"
    assert len(data["tasks"]) == 2


def test_on_request_replies_with_served_by(server: ApiServer):
    import asyncio

    published: list[tuple[str, bytes]] = []

    class FakeNc:
        async def publish(self, subject: str, payload: bytes) -> None:
            published.append((subject, payload))

    class FakeBusWithNc(FakeBus):
        nc = FakeNc()

    server2 = ApiServer(server.settings, FakeBusWithNc())  # type: ignore[arg-type]
    req = json.dumps({"verb": "node.detail", "args": {"node_id": "node-b"}}).encode()
    asyncio.run(server2._on_request(FakeMsg(req)))
    subject, payload = published[0]
    assert subject == "inbox.test"
    resp = json.loads(payload)
    assert resp["ok"] is True
    assert resp["served_by"] == "test-node"
    assert resp["data"]["node"]["node_id"] == "node-b"


def test_on_request_error_still_replies(server: ApiServer):
    import asyncio

    published: list[tuple[str, bytes]] = []

    class FakeNc:
        async def publish(self, subject: str, payload: bytes) -> None:
            published.append((subject, payload))

    class FakeBusWithNc(FakeBus):
        nc = FakeNc()

    server2 = ApiServer(server.settings, FakeBusWithNc())  # type: ignore[arg-type]
    req = json.dumps({"verb": "nope"}).encode()
    asyncio.run(server2._on_request(FakeMsg(req)))
    resp = json.loads(published[0][1])
    assert resp["ok"] is False and "error" in resp


def test_format_node_detail_shows_content():
    from memnc.telegram import format_node_detail

    data = {
        "node": {
            "node_id": "node-a",
            "roles": ["coder"],
            "models": ["m1"],
            "gpu": "-",
        },
        "tasks": [
            {
                "task_id": "1a1d166832a50a25b54c376",
                "state": "CODING",
                "claimed_by": "node-a-coder:coder",
                "task": "Write a one-line haiku about mesh\nmore context",
            }
        ],
    }
    out = format_node_detail(data)
    assert "node node-a roles=coder" in out
    assert "1 tasks:" in out
    assert "#1a1d166832a5 CODING by node-a-coder:coder" in out
    assert "Write a one-line haiku about mesh" in out


# -- M11c: resource / governance read verbs (console proxy) -------------------


class FakeRegBus(FakeBus):
    """Adds the governance reads the console collect_state proxies."""

    def __init__(self) -> None:
        super().__init__()
        self.cap = [Capability(agent_id="a1", skill="coder", node="node-a")]
        self.grants = [GrantRecord(agent_id="a1", level=GrantLevel.ROOT)]
        self.trust = [TrustEntry(subject="a1", credit=0.9, rating="GOOD")]
        self.ledger = {"tasks": 3, "grants": 1}

    async def list_capabilities(self):
        return self.cap

    async def list_grants(self):
        return self.grants

    async def list_trust(self):
        return self.trust

    async def read_ledger(self):
        return self.ledger


def test_read_verbs_expose_governance_state():
    from memnc.config import Settings

    srv = ApiServer(Settings(node_name="n"), FakeRegBus())  # type: ignore[arg-type]
    caps = asyncio.run(srv.dispatch("cap.list", {}))
    grants = asyncio.run(srv.dispatch("grant.list", {}))
    trust = asyncio.run(srv.dispatch("trust.list", {}))
    ledger = asyncio.run(srv.dispatch("ledger.read", {}))
    assert caps[0]["agent_id"] == "a1" and caps[0]["skill"] == "coder"
    assert grants[0]["level"] == "root"
    assert trust[0]["credit"] == 0.9
    assert ledger == {"tasks": 3, "grants": 1}


def test_console_write_actions_map_to_api_verbs():
    from memnc.console import ConsoleHandler

    src = inspect.getsource(ConsoleHandler._control)
    for action, verb in [
        ("cancel", "task.cancel"),
        ("retry", "task.retry"),
        ("pause_node", "node.pause_node"),
        ("resume_node", "node.resume_node"),
    ]:
        assert f'"{action}": "{verb}"' in src
