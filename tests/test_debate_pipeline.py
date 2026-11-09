"""T5: the M14 self-pipeline emits debate.code/debate.critique events."""

from __future__ import annotations

import asyncio

from memnc.config import Settings
from memnc.schema import (
    CoderOutput,
    CriticOutput,
    FileArtifact,
    PlanStep,
    PlannerOutput,
    ResultPayload,
    Role,
    TaskState,
)
from memnc.worker import Worker

from test_m14 import SilentBus, _worker
from test_m11_claim import _snapshot


def test_pipeline_streams_debate_events():
    bus = SilentBus(Settings())
    snap = _snapshot(
        "t-t5-1", state=TaskState.RECEIVED, claimable=True, next_role="planner"
    )
    asyncio.run(bus.put_snapshot(snap))

    plan = PlannerOutput(plan=[PlanStep(id="s1", description="write it")], notes="")
    planner = _worker(
        Role.PLANNER, bus, ResultPayload(ok=True, output=plan.model_dump())
    )
    asyncio.run(planner._self_pipeline(asyncio.run(bus.try_claim(
        "t-t5-1", "b:planner", role="planner"))))

    # planner output is not part of the debate — no debate event yet
    assert not any(e[0].startswith("debate.") for e in bus.events)

    code = CoderOutput(
        summary="done",
        files=[FileArtifact(path="hello.py", content="print('hi')")],
    )
    coder = _worker(Role.CODER, bus, ResultPayload(ok=True, output=code.model_dump()))
    asyncio.run(coder._self_pipeline(asyncio.run(bus.try_claim(
        "t-t5-1", "b:coder", role="coder"))))

    debate = [e for e in bus.events if e[0] == "debate.code"]
    assert len(debate) == 1
    assert "done" in debate[0][1]
    assert "hello.py" in debate[0][1]

    critic = _worker(
        Role.CRITIC,
        bus,
        ResultPayload(
            ok=True, output=CriticOutput(approved=True, summary="fine").model_dump()
        ),
    )
    asyncio.run(critic._self_pipeline(asyncio.run(bus.try_claim(
        "t-t5-1", "a:critic", role="critic"))))

    critique = [e for e in bus.events if e[0] == "debate.critique"]
    assert len(critique) == 1
    assert "APPROVED" in critique[0][1]

    # debate events are replayable from the snapshot history
    after = asyncio.run(bus.get_snapshot("t-t5-1"))
    assert after.state is TaskState.DONE
    history = [h.event for h in after.history]
    assert "debate.code" in history
    assert "debate.critique" in history
