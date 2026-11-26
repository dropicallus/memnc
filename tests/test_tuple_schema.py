"""Structured communication tuple models (docs/roadmap.md)."""

import asyncio
import json

from memnc.config import Settings
from memnc.roles import build_user_prompt
from memnc.schema import (
    Background,
    CriticIssue,
    CriticOutput,
    Envelope,
    FileArtifact,
    IntermediateOutput,
    Kind,
    MessageTuple,
    PlannerOutput,
    PlanStep,
    Role,
    TaskSnapshot,
    TaskState,
)


def test_background_defaults():
    bg = Background(task="fix the bug")
    assert bg.task == "fix the bug"
    assert bg.context == ""
    assert bg.plan is None
    assert bg.assumptions == []


def test_intermediate_output_coder():
    io = IntermediateOutput(
        role=Role.CODER,
        summary="patched",
        files=[FileArtifact(path="a.py", content="x = 1")],
    )
    assert io.role == Role.CODER
    assert io.issues == []


def test_intermediate_output_critic_issues():
    io = IntermediateOutput(
        role=Role.CRITIC,
        summary="blocked",
        issues=[CriticIssue(file="a.py", line=3, severity="blocker", issue="oops")],
    )
    assert io.issues[0].severity == "blocker"


def test_message_tuple_round_trip():
    bg = Background(task="t", context="c", assumptions=["a1"])
    io = IntermediateOutput(role=Role.PLANNER, summary="planned")
    tup = MessageTuple(message="code this", background=bg, intermediate_output=io)
    data = json.loads(tup.model_dump_json())
    tup2 = MessageTuple.model_validate(data)
    assert tup2 == tup
    assert tup2.intermediate_output is not None
    assert tup2.intermediate_output.role == Role.PLANNER


def test_background_plan_round_trip():
    plan = PlannerOutput(plan=[PlanStep(description="do it")])
    bg = Background(task="t", plan=plan)
    data = json.loads(bg.model_dump_json())
    bg2 = Background.model_validate(data)
    assert bg2 == bg
    assert bg2.plan is not None
    assert bg2.plan.plan[0].description == "do it"


def test_message_tuple_intermediate_output_optional():
    tup = MessageTuple(message="hello", background=Background(task="t"))
    assert tup.intermediate_output is None


# -- orchestration wiring (steps 2+3 of the tuple-schema slice) --------------


class _CaptureBus:
    """Minimal fake bus: snapshot store + captured command publishes."""

    def __init__(self) -> None:
        self.snapshots: dict[str, TaskSnapshot] = {}
        self.published: list[tuple[str, Envelope]] = []
        self.backgrounds: dict[str, Background] = {}

    async def get_snapshot(self, task_id):
        return self.snapshots.get(task_id)

    async def put_snapshot(self, snapshot):
        self.snapshots[snapshot.task_id] = snapshot

    async def list_tasks(self):
        return list(self.snapshots.values())

    async def emit_event(self, snapshot, event, detail=""):
        pass

    async def publish_envelope(self, subject, envelope):
        self.published.append((subject, envelope))

    async def save_background(self, task_id, background):
        self.backgrounds[task_id] = background

    async def get_background(self, task_id):
        return self.backgrounds.get(task_id)


def _snapshot(state=TaskState.PLANNING) -> TaskSnapshot:
    snap = TaskSnapshot(
        task_id="tup-flow-1",
        trace_id="trace-1",
        task="fix the login bug",
        context="repo uses jwt tokens",
        state=state,
    )
    snap.plan = PlannerOutput(plan=[PlanStep(description="write test")])
    return snap


def test_dispatch_attaches_tuple_when_flag_on():
    from memnc.orchestrator import Orchestrator

    settings = Settings(tuple_prompts=True)
    orchestrator = Orchestrator(settings)
    bus = _CaptureBus()
    orchestrator.bus = bus  # type: ignore[assignment]
    snap = _snapshot()
    bus.snapshots[snap.task_id] = snap
    asyncio.run(orchestrator._dispatch(snap, Role.PLANNER))
    assert bus.published, "no command published"
    _, env = bus.published[0]
    tup = env.payload["message_tuple"]
    assert tup["message"] == "Produce a plan for the task."
    assert tup["background"]["task"] == "fix the login bug"
    assert tup["background"]["context"] == "repo uses jwt tokens"
    assert tup["background"]["plan"]["plan"][0]["description"] == "write test"
    assert tup["intermediate_output"] is None


def test_dispatch_without_flag_has_no_tuple_key():
    from memnc.orchestrator import Orchestrator

    orchestrator = Orchestrator(Settings())
    bus = _CaptureBus()
    orchestrator.bus = bus  # type: ignore[assignment]
    snap = _snapshot()
    bus.snapshots[snap.task_id] = snap
    asyncio.run(orchestrator._dispatch(snap, Role.PLANNER))
    _, env = bus.published[0]
    assert "message_tuple" not in env.payload


def test_coder_dispatch_carries_planner_output_as_io():
    from memnc.orchestrator import Orchestrator

    settings = Settings(tuple_prompts=True)
    orchestrator = Orchestrator(settings)
    bus = _CaptureBus()
    orchestrator.bus = bus  # type: ignore[assignment]
    snap = _snapshot()
    snap.critique = CriticOutput(verdict="retry", summary="close but wrong", issues=[CriticIssue(issue="off by one")])
    bus.snapshots[snap.task_id] = snap
    asyncio.run(orchestrator._dispatch(snap, Role.CODER))
    _, env = bus.published[0]
    io = env.payload["message_tuple"]["intermediate_output"]
    assert io["role"] == "critic"
    assert io["issues"][0]["issue"] == "off by one"


def test_save_background_persists_via_bus():
    from memnc.orchestrator import Orchestrator

    orchestrator = Orchestrator(Settings())
    bus = _CaptureBus()
    orchestrator.bus = bus  # type: ignore[assignment]
    snap = _snapshot()
    asyncio.run(orchestrator._save_background(snap))
    bg = asyncio.run(bus.get_background(snap.task_id))
    assert bg is not None
    assert bg.task == "fix the login bug"
    assert bg.plan is not None


def test_worker_prompt_uses_tuple_when_attached():
    from memnc.worker import Worker

    raw = {
        "message": "Produce a plan for the task.",
        "background": {"task": "fix the login bug", "context": "jwt repo"},
    }
    env = Envelope(
        task_id="tup-flow-1",
        trace_id="trace-1",
        kind=Kind.COMMAND,
        role=Role.PLANNER,
        payload={"message_tuple": raw},
    )
    prompt = Worker._message_tuple(env)
    assert prompt is not None
    rendered = build_user_prompt(Role.PLANNER, {}, prompt)
    assert "MESSAGE:" in rendered
    assert "Produce a plan for the task." in rendered
    assert "BACKGROUND (shared, read-only):" in rendered
    assert "fix the login bug" in rendered
    # prose sections are replaced, not duplicated
    assert "ADDITIONAL CONTEXT:" not in rendered


def test_worker_prompt_falls_back_without_tuple():
    from memnc.worker import Worker

    env = Envelope(
        task_id="tup-flow-1",
        trace_id="trace-1",
        kind=Kind.COMMAND,
        role=Role.PLANNER,
        payload={"task": "plain prose task"},
    )
    assert Worker._message_tuple(env) is None
