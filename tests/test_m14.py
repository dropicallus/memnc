"""M14: worker-driven pipeline — a claimed worker runs its role and hands the
task to the next role by making it claimable (no orchestrator dispatch)."""

from __future__ import annotations

import asyncio

from test_m11_claim import FakeKV, _snapshot

from memnc.bus import Bus
from memnc.config import Settings
from memnc.schema import (
    CoderOutput,
    CriticOutput,
    Envelope,
    FileArtifact,
    PlannerOutput,
    PlanStep,
    ResultPayload,
    Role,
    TaskSnapshot,
    TaskState,
)
from memnc.worker import Worker


class SilentBus(Bus):
    """Bus with the fake KV but no NATS connection."""

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.kv = FakeKV()  # type: ignore[assignment]
        self.events: list[tuple[str, str]] = []

    async def publish_envelope(self, subject, envelope):  # type: ignore[override]
        return None

    async def emit_event(self, snapshot, event, detail=""):  # type: ignore[override]
        self.events.append((event, detail))
        return None


def _worker(role: Role, bus: SilentBus, output: ResultPayload) -> Worker:
    settings = Settings(
        role=role.value,
        mesh_claim=True,
        self_pipeline=True,
        apply_artifacts=False,
    )
    w = Worker(settings, bus=bus)

    async def fake_run(envelope: Envelope, snapshot: TaskSnapshot) -> ResultPayload:
        return output

    w._run = fake_run  # type: ignore[method-assign]
    return w


def test_self_pipeline_runs_full_task_without_orchestrator():
    bus = SilentBus(Settings())
    snap = _snapshot(
        "t-m14-1",
        state=TaskState.RECEIVED,
        claimable=True,
        next_role="planner",
    )
    asyncio.run(bus.put_snapshot(snap))

    plan = PlannerOutput(
        plan=[PlanStep(id="s1", description="write it")], notes=""
    )
    planner = _worker(Role.PLANNER, bus, ResultPayload(ok=True, output=plan.model_dump()))
    claimed = asyncio.run(bus.try_claim("t-m14-1", "b:planner", role="planner"))
    assert claimed is not None
    asyncio.run(planner._self_pipeline(claimed))

    after = asyncio.run(bus.get_snapshot("t-m14-1"))
    assert after is not None
    assert after.state is TaskState.CODING
    assert after.next_role == "coder"
    assert after.claimable and after.claimed_by == ""
    assert after.plan is not None and after.plan.plan[0].id == "s1"

    # only the coder may claim now; a planner is rejected (role gate)
    assert asyncio.run(bus.try_claim("t-m14-1", "b:planner", role="planner")) is None

    code = CoderOutput(
        summary="done",
        files=[FileArtifact(path="hello.py", content="print('hi')")],
    )
    coder = _worker(Role.CODER, bus, ResultPayload(ok=True, output=code.model_dump()))
    claimed = asyncio.run(bus.try_claim("t-m14-1", "b:coder", role="coder"))
    assert claimed is not None
    asyncio.run(coder._self_pipeline(claimed))

    after = asyncio.run(bus.get_snapshot("t-m14-1"))
    assert after.state is TaskState.CRITIQUE
    assert after.next_role == "critic"

    critic = _worker(
        Role.CRITIC,
        bus,
        ResultPayload(
            ok=True, output=CriticOutput(approved=True, summary="fine").model_dump()
        ),
    )
    claimed = asyncio.run(bus.try_claim("t-m14-1", "a:critic", role="critic"))
    assert claimed is not None
    asyncio.run(critic._self_pipeline(claimed))

    after = asyncio.run(bus.get_snapshot("t-m14-1"))
    assert after.state is TaskState.DONE
    assert after.next_role == ""
    assert not after.claimable


def test_self_pipeline_failed_result_retries_then_fails():
    bus = SilentBus(Settings())
    snap = _snapshot(
        "t-m14-2",
        state=TaskState.RECEIVED,
        claimable=True,
        next_role="planner",
    )
    asyncio.run(bus.put_snapshot(snap))

    failing = _worker(
        Role.PLANNER, bus, ResultPayload(ok=False, error="boom")
    )
    claimed = asyncio.run(bus.try_claim("t-m14-2", "b:planner", role="planner"))
    assert claimed is not None
    asyncio.run(failing._self_pipeline(claimed))

    after = asyncio.run(bus.get_snapshot("t-m14-2"))
    # attempt 1 of 3: back to a dispatchable planner (retry handoff)
    assert after.state in (TaskState.PLANNING, TaskState.RECEIVED)
    assert after.next_role == "planner"
    assert after.claimable and after.claimed_by == ""
    assert after.attempt == 1

    # exhaust attempts -> FAILED
    for i in range(2, 5):
        claimed = asyncio.run(
            bus.try_claim("t-m14-2", f"b:planner-{i}", role="planner")
        )
        if claimed is None:
            break
        asyncio.run(failing._self_pipeline(claimed))
    after = asyncio.run(bus.get_snapshot("t-m14-2"))
    assert after.state is TaskState.FAILED
    assert not after.claimable


def test_critic_rejection_sends_task_to_revise():
    bus = SilentBus(Settings())
    plan = PlannerOutput(plan=[PlanStep(id="s1", description="x")])
    code = CoderOutput(summary="done", files=[])
    # task already planned+coded, waiting for critic
    snap = _snapshot(
        "t-m14-3",
        state=TaskState.CRITIQUE,
        next_role="critic",
        claimable=True,
        plan=plan,
        code=code,
        revisions=0,
    )
    asyncio.run(bus.put_snapshot(snap))

    reject = _worker(
        Role.CRITIC,
        bus,
        ResultPayload(
            ok=True,
            output=CriticOutput(approved=False, summary="no").model_dump(),
        ),
    )
    claimed = asyncio.run(bus.try_claim("t-m14-3", "a:critic", role="critic"))
    assert claimed is not None
    asyncio.run(reject._self_pipeline(claimed))

    after = asyncio.run(bus.get_snapshot("t-m14-3"))
    assert after.revisions == 1
    # REVISE immediately applies "start": the coder is re-dispatched
    assert after.state is TaskState.CODING
    assert after.next_role == "coder"
    assert after.claimable
