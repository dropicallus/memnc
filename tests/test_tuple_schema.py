"""Structured communication tuple models (docs/design-message-tuple.md)."""

import json

from memnc.schema import (
    Background,
    CriticIssue,
    FileArtifact,
    IntermediateOutput,
    MessageTuple,
    PlannerOutput,
    PlanStep,
    Role,
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
