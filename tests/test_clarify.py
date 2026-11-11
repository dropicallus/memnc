"""M16 mesh-clarify: peers get one consult round before the human is asked."""

from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import ValidationError

from memnc import clarify
from memnc.config import Settings
from memnc.schema import (
    SUBJECT_CMD,
    ConsultAnswer,
    ConsultRequest,
    Envelope,
    Kind,
    Role,
    TaskSnapshot,
    TaskState,
)


def _snapshot(task_id: str = "t-clar", **fields) -> TaskSnapshot:
    fields.setdefault("state", TaskState.WAIT_INPUT)
    fields.setdefault("question", "which port should the server use?")
    fields.setdefault("question_role", Role.CODER)
    return TaskSnapshot(
        task_id=task_id, trace_id="tr-1", task="do a thing", **fields
    )


class FakeBus:
    """Records published command envelopes; no NATS."""

    def __init__(self):
        self.commands: list[tuple[str, Envelope]] = []
        self._snapshot = None

    async def publish_envelope(self, subject, envelope):
        self.commands.append((subject, envelope))
        return None

    async def emit_event(self, snapshot, event, detail=""):
        return None

    async def put_snapshot(self, snapshot):
        self._snapshot = snapshot
        return None

    async def get_snapshot(self, task_id):
        return getattr(self, "_snapshot", None)


# --- schema models -------------------------------------------------------


def test_consult_request_holds_question_and_role():
    request = ConsultRequest(question="what?", from_role="coder")
    assert request.question == "what?"
    assert request.from_role == "coder"


def test_consult_answer_requires_answer_or_cannot_answer():
    assert ConsultAnswer(answer="port 8080").answer == "port 8080"
    assert ConsultAnswer(cannot_answer="human choice").cannot_answer


def test_consult_answer_empty_rejected():
    with pytest.raises(ValidationError, match="consult answer needs"):
        ConsultAnswer(answer="", cannot_answer="")


def test_consult_answer_prefers_answer_when_both_set():
    answer = ConsultAnswer(answer="yes", cannot_answer="no")
    assert answer.answer == "yes"


# --- consult dispatch ----------------------------------------------------


def test_start_publishes_one_consult_per_peer_role():
    bus = FakeBus()
    settings = Settings(max_clarify_rounds=1)
    snapshot = _snapshot(question_role=Role.CODER)

    asyncio.run(clarify.start(bus, settings, snapshot))

    subjects = [s for s, _ in bus.commands]
    assert subjects == [SUBJECT_CMD.format(role="planner")]  # critic skipped: no critique
    kinds = [e.kind for _, e in bus.commands]
    assert kinds == [Kind.COMMAND]
    for _, envelope in bus.commands:
        consult = envelope.payload["consult"]
        assert consult["question"] == "which port should the server use?"
        assert consult["from_role"] == "coder"
        assert envelope.task_id == "t-clar"
    assert snapshot.clarify_started is True
    assert snapshot.clarify_round == 0
    assert snapshot.pending_consults == ["planner"]


def test_start_skips_critic_without_critique():
    bus = FakeBus()
    snapshot = _snapshot(question_role=Role.PLANNER)

    asyncio.run(clarify.start(bus, Settings(), snapshot))

    peers = [e.role.value for _, e in bus.commands]
    assert peers == ["coder"]  # coder + critic peers


def test_start_declined_after_budget_spent():
    bus = FakeBus()
    snapshot = _snapshot(clarify_round=1)

    asyncio.run(clarify.start(bus, Settings(max_clarify_rounds=1), snapshot))

    assert bus.commands == []
    assert snapshot.clarify_started is False


def test_start_noop_without_pending_question():
    bus = FakeBus()
    snapshot = TaskSnapshot(
        task_id="t-0", trace_id="tr-1", task="do a thing",
        state=TaskState.WAIT_INPUT, question="",
    )

    asyncio.run(clarify.start(bus, Settings(), snapshot))

    assert bus.commands == []


def test_start_noop_when_already_started():
    bus = FakeBus()
    snapshot = _snapshot(clarify_started=True)

    asyncio.run(clarify.start(bus, Settings(), snapshot))

    assert bus.commands == []


# --- consult collection --------------------------------------------------


def _consult_envelope(
    consult: dict, role: Role, node: str = "node-b"
) -> Envelope:
    return Envelope(
        id=f"env-{node}",
        kind=Kind.RESULT,
        trace_id="tr-1",
        role=role,
        node=node,
        task_id="t-clar",
        payload={"ok": True, "output": {"consult": consult}},
    )


def test_handle_consult_with_peer_answer_appends_and_resumes():
    bus = FakeBus()
    snapshot = _snapshot(clarify_started=True, pending_consults=["planner"])
    before = snapshot.task

    asyncio.run(
        clarify.handle_consult(
            bus,
            snapshot,
            _consult_envelope(
                {"answer": "use 8080, it is free"}, Role.PLANNER
            ),
        )
    )

    assert snapshot.state is TaskState.CODING  # dispatch to the asking role
    assert snapshot.claimable is True
    assert "use 8080" in snapshot.task
    assert snapshot.peer_answers == {"planner": "use 8080, it is free"}
    assert snapshot.task.startswith(before)
    assert snapshot.question == ""


def test_handle_consult_cannot_answer_leaves_question_with_human():
    bus = FakeBus()
    snapshot = _snapshot(clarify_started=True, pending_consults=["coder"])

    asyncio.run(
        clarify.handle_consult(
            bus,
            snapshot,
            _consult_envelope(
                {"cannot_answer": "needs the human's preference"}, Role.CODER
            ),
        )
    )

    assert snapshot.state is TaskState.WAIT_INPUT
    assert snapshot.claimable is False
    assert snapshot.peer_answers == {"coder": ""}
    assert not snapshot.clarify_started  # round closed, question stays


def test_handle_consult_ignores_unknown_consult_result():
    bus = FakeBus()
    snapshot = _snapshot(clarify_started=True, pending_consults=[])
    snapshot.peer_answers = {"coder": "earlier"}

    asyncio.run(
        clarify.handle_consult(
            bus,
            snapshot,
            _consult_envelope({"answer": "late"}, Role.PLANNER, node="x2"),
        )
    )

    assert snapshot.peer_answers == {"coder": "earlier"}  # late result ignored


def test_handle_consult_invalid_result_leaves_question_with_human():
    bus = FakeBus()
    snapshot = _snapshot(clarify_started=True, pending_consults=["planner"])

    asyncio.run(
        clarify.handle_consult(
            bus,
            snapshot,
            _consult_envelope({"bad": "payload"}, Role.PLANNER),
        )
    )

    assert snapshot.state is TaskState.WAIT_INPUT
    assert not snapshot.clarify_started  # round closed, question stays


# --- orchestrator routing ------------------------------------------------


def test_orchestrator_routes_consult_results_to_clarify():
    from memnc.orchestrator import Orchestrator

    bus = FakeBus()
    orch = Orchestrator(settings=Settings(), bus=bus)
    snapshot = _snapshot(clarify_started=True, pending_consults=["planner"])
    envelope = _consult_envelope(
        {"answer": "use 8080, it is free"}, Role.PLANNER
    )
    message = type("M", (), {"data": json.dumps(envelope.model_dump()).encode()})()
    asyncio.run(bus.put_snapshot(snapshot))

    asyncio.run(orch._handle_result(message))

    assert snapshot.peer_answers == {"planner": "use 8080, it is free"}
    assert snapshot.state is TaskState.CODING


# --- worker consult run --------------------------------------------------


def test_worker_run_consult_uses_peer_prompt_and_parses_json():
    from memnc.worker import Worker

    bus = FakeBus()
    settings = Settings(role="critic")
    worker = Worker(settings, bus=bus)

    captured: dict = {}

    async def fake_run_agent(**kwargs):
        captured.update(kwargs)
        content = json.dumps({"answer": "port 8080"})
        return type("R", (), {"content": content})()

    import memnc.worker as worker_mod

    worker_mod.run_agent = fake_run_agent  # type: ignore[assignment]

    envelope = Envelope(
        id="env-1",
        kind=Kind.COMMAND,
        trace_id="tr-1",
        role=Role.CRITIC,
        task_id="t-clar",
        payload={
            "consult": {"question": "which port?", "from_role": "coder"},
            "task": "run a server",
        },
    )
    result = asyncio.run(
        worker._run_consult(envelope, Role.CRITIC, "fake-model", {
            "question": "which port?", "from_role": "coder"
        })
    )

    assert result.ok is True, result.error
    assert result.output["consult"]["answer"] == "port 8080"
    assert "which port?" in captured["messages"][1]["content"]
    assert captured["messages"][0]["role"] == "system"


def test_worker_run_consult_invalid_json_is_declined():
    from memnc.worker import Worker

    bus = FakeBus()
    worker = Worker(Settings(role="critic"), bus=bus)

    async def fake_run_agent(**kwargs):
        return type("R", (), {"content": "not json"})()

    import memnc.worker as worker_mod

    worker_mod.run_agent = fake_run_agent  # type: ignore[assignment]

    result = asyncio.run(
        worker._run_consult(
            Envelope(id="e", trace_id="tr-1", kind=Kind.COMMAND,
                     role=Role.CRITIC, task_id="t", payload={}),
            Role.CRITIC,
            "m",
            {"question": "q?", "from_role": "coder"},
        )
    )

    assert result.ok is False
    assert "invalid consult answer" in (result.error or "")


def test_worker_run_consult_bad_request_is_declined():
    from memnc.worker import Worker

    worker = Worker(Settings(role="critic"), bus=FakeBus())
    result = asyncio.run(
        worker._run_consult(
            Envelope(id="e", trace_id="tr-1", kind=Kind.COMMAND,
                     role=Role.CRITIC, task_id="t", payload={}),
            Role.CRITIC,
            "m",
            {"from_role": "coder"},  # missing question
        )
    )
    assert result.ok is False
    assert "invalid consult request" in (result.error or "")
