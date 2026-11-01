import pytest
from pydantic import ValidationError

from memnc.schema import (
    CoderOutput,
    CriticOutput,
    Envelope,
    PlannerOutput,
    ResultPayload,
    TaskSnapshot,
    TaskSubmission,
)


def test_envelope_roundtrip():
    envelope = Envelope(task_id="t1", trace_id="tr1", kind="command", role="coder")
    restored = Envelope.model_validate_json(envelope.model_dump_json())
    assert restored.task_id == "t1"
    assert restored.role.value == "coder"
    assert restored.id == envelope.id


def test_envelope_rejects_empty_ids():
    with pytest.raises(ValidationError):
        Envelope(task_id="", trace_id="tr", kind="event")


def test_planner_output_requires_steps():
    with pytest.raises(ValidationError):
        PlannerOutput(plan=[])
    output = PlannerOutput.model_validate(
        {"plan": [{"step": 1, "description": "do it", "files": ["a.py"]}]}
    )
    assert output.plan[0].files == ["a.py"]


def test_coder_output_roundtrip():
    output = CoderOutput.model_validate(
        {"summary": "s", "files": [{"path": "a.py", "content": "x=1"}], "commands": []}
    )
    assert output.files[0].path == "a.py"


def test_critic_output_defaults():
    output = CriticOutput.model_validate({"approved": False})
    assert output.issues == []
    assert output.approved is False


def test_submission_rejects_blank_task():
    with pytest.raises(ValidationError):
        TaskSubmission(task="   ")


def test_snapshot_history_is_trimmed():
    snapshot = TaskSnapshot(task_id="t", trace_id="tr", task="x")
    for index in range(150):
        snapshot.record("event", str(index))
    assert len(snapshot.history) == 100


def test_result_payload_requires_ok():
    with pytest.raises(ValidationError):
        ResultPayload.model_validate({"output": {}})
