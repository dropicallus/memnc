"""T4 (model-routing-visible): the task snapshot records node:model per role.

Both result paths must fill TaskSnapshot.route so the console/trace show
which model actually handled each pipeline stage:
- orchestrator._handle_result (legacy dispatch path)
- worker._advance (self_pipeline path)
"""

from __future__ import annotations

import asyncio
import json

from test_m10 import FakeBus, _FakeMsg, _result_envelope

from memnc.config import Settings
from memnc.orchestrator import Orchestrator
from memnc.schema import (
    ResultPayload,
    Role,
    TaskSnapshot,
    TaskState,
)
from memnc.worker import Worker


async def _capture_publish(self, subject, envelope):  # type: ignore[no-untyped-def]
    self.events.append(("publish", subject))
    self.published = getattr(self, "published", [])
    self.published.append((subject, envelope))


FakeBus.publish_envelope = _capture_publish  # type: ignore[attr-defined]


def _snapshot(task_id: str = "route-1") -> TaskSnapshot:
    return TaskSnapshot(
        task_id=task_id,
        trace_id="trace-1",
        task="route me",
        state=TaskState.PLANNING,
    )


def test_orchestrator_records_route_from_result():
    orchestrator = Orchestrator(Settings())
    orchestrator.bus = FakeBus()  # type: ignore[assignment]
    snapshot = _snapshot()
    orchestrator.bus.snapshots[snapshot.task_id] = snapshot

    envelope = _result_envelope(snapshot.task_id, role=Role.PLANNER)
    envelope.payload = ResultPayload(ok=True, output={}, model="llama3.2:3b").model_dump()
    msg = _FakeMsg(json.dumps(envelope.model_dump()).encode())
    asyncio.run(orchestrator._handle_result(msg))

    assert snapshot.route == {"planner": "node-a:llama3.2:3b"}


def test_worker_records_route_on_advance():
    settings = Settings(role="coder")
    bus = FakeBus()  # type: ignore[assignment]
    worker = Worker(settings, bus=bus)  # type: ignore[arg-type]
    snapshot = _snapshot("route-2")
    bus.snapshots[snapshot.task_id] = snapshot

    envelope = _result_envelope(snapshot.task_id, role=Role.CODER)
    envelope.payload = ResultPayload(ok=True, output={}, model="qwen2.5:7b").model_dump()

    async def scenario():
        # _run stamps the executed model onto the result (T4)
        async def fake_run_impl(env, snap):
            return ResultPayload(ok=True, output={})

        worker._run_impl = fake_run_impl  # type: ignore[method-assign]
        stamped = await worker._run(envelope, snapshot)
        assert stamped.model == settings.model_for(Role.CODER)
        await worker._advance(snapshot, envelope, stamped)

    asyncio.run(scenario())

    stored = bus.snapshots[snapshot.task_id]
    assert stored.route == {
        "coder": f"{settings.node_name}:{settings.model_coder}"
    }


def test_route_absent_when_result_has_no_model():
    """Old workers (pre-T4 results) must not crash or write bogus routes."""
    orchestrator = Orchestrator(Settings())
    orchestrator.bus = FakeBus()  # type: ignore[assignment]
    snapshot = _snapshot("route-3")
    orchestrator.bus.snapshots[snapshot.task_id] = snapshot

    msg = _FakeMsg(json.dumps(_result_envelope(snapshot.task_id).model_dump()).encode())
    asyncio.run(orchestrator._handle_result(msg))

    assert snapshot.route == {}
