"""Tests for the M11a mesh API read server (src/memnc/api.py)."""

from __future__ import annotations

import json

import pytest

from memnc.api import ApiServer
from memnc.schema import NodeInfo, TaskSnapshot, TaskState


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
        return next(
            (s for s in self.snaps if s.task_id == task_id), None
        )

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
