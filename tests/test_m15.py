"""M15: symmetric orchestration — owner routing and origin stamping."""

import asyncio

from test_m11 import FakeBus

from memnc.config import Settings
from memnc.orchestrator import Orchestrator
from memnc.schema import SUBJECT_SUBMIT_NODE, TaskSnapshot, TaskState, TaskSubmission


def _orch(node: str) -> Orchestrator:
    settings = Settings()
    settings.node_name = node
    return Orchestrator(settings, FakeBus())


def test_owns_accepts_legacy_and_own_origin():
    a = _orch("node-a")
    legacy = TaskSnapshot(task_id="t1", trace_id="x", task="t")
    own = TaskSnapshot(task_id="t2", trace_id="x", task="t", origin_node="node-a")
    foreign = TaskSnapshot(task_id="t3", trace_id="x", task="t", origin_node="node-b")
    assert a._owns(legacy)
    assert a._owns(own)
    assert not a._owns(foreign)


def test_ownership_guards_skip_foreign_tasks():
    a = _orch("node-a")
    foreign = TaskSnapshot(
        task_id="t1",
        trace_id="x",
        task="t",
        origin_node="node-b",
        state=TaskState.RECEIVED,
    )
    a.bus.snapshots["t1"] = foreign
    asyncio.run(a._dispatch_claimed())
    assert a.bus.snapshots["t1"].state is TaskState.RECEIVED


def test_submit_stamps_origin_node():
    a = _orch("node-a")

    class _FakeMsg:
        data: bytes = b""

    async def fake_submit(subject, data, timeout=10):
        return _FakeMsg()

    a.bus.nc.request = fake_submit  # type: ignore[method-assign]
    msg = type("M", (), {"data": TaskSubmission(task="hello").model_dump_json().encode(),
                         "subject": SUBJECT_SUBMIT_NODE.format(node="node-a")})()
    asyncio.run(a._on_submit(msg))
    task_id = next(iter(a.bus.snapshots))
    assert a.bus.snapshots[task_id].origin_node == "node-a"
