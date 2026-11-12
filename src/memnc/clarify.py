"""M16 mesh-clarify: peers get one consult round before the human is asked.

When an agent asks a clarifying question instead of producing output, the
task parks in WAIT_INPUT. Before the question is left to a human, the other
pipeline roles are consulted: peers that can resolve the question from the
task context answer it, the compiled answers are appended to the task text,
and the asking role is resumed. Only when the peers decline (or the consult
budget is spent) does the question stay with the human.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from .fsm import Event
from .schema import (
    SUBJECT_CMD,
    ConsultAnswer,
    Envelope,
    Kind,
    Role,
    TaskSnapshot,
    TaskState,
)

log = logging.getLogger(__name__)

ApplyFn = Callable[[TaskSnapshot, Event], Awaitable[None]]


async def start(bus, settings, snapshot: TaskSnapshot) -> None:
    """Consult the peer roles about the parked question (best effort)."""
    if snapshot.clarify_started or snapshot.state is not TaskState.WAIT_INPUT:
        return
    if not snapshot.question:
        return
    if snapshot.clarify_round >= settings.max_clarify_rounds:
        await bus.emit_event(
            snapshot,
            "clarify.exhausted",
            "peers could not clarify; waiting for a human answer",
        )
        await bus.put_snapshot(snapshot)
        return

    snapshot.clarify_started = True
    asker = snapshot.question_role or Role.PLANNER
    peers = [
        role
        for role in (Role.PLANNER, Role.CODER, Role.CRITIC)
        if role is not asker and not (role is Role.CRITIC and not snapshot.critique)
    ]
    snapshot.pending_consults = [peer.value for peer in peers]
    snapshot.peer_answers = {}
    await bus.put_snapshot(snapshot)

    for peer in peers:
        command = Envelope(
            task_id=snapshot.task_id,
            trace_id=snapshot.trace_id,
            kind=Kind.COMMAND,
            role=peer,
            node=settings.node_name,
            state=snapshot.state,
            attempt=snapshot.attempt,
            timeout_ms=settings.clarify_timeout_s * 1000,
            payload={
                "task": snapshot.task,
                "context": snapshot.context,
                "plan": snapshot.plan.model_dump() if snapshot.plan else None,
                "code": snapshot.code.model_dump() if snapshot.code else None,
                "critique": snapshot.critique.model_dump() if snapshot.critique else None,
                "consult": {
                    "question": snapshot.question,
                    "from_role": asker.value,
                },
            },
        )
        await bus.publish_envelope(SUBJECT_CMD.format(role=peer.value), command)
        await bus.emit_event(snapshot, "clarify.dispatch", f"{asker.value} asks {peer.value}")

    asyncio.get_running_loop().create_task(_deadline(bus, settings, snapshot.task_id))


async def _deadline(bus, settings, task_id: str) -> None:
    """Finish the clarify round even when a peer never answers."""
    await asyncio.sleep(settings.clarify_timeout_s + 30)
    snapshot = await bus.get_snapshot(task_id)
    if (
        snapshot is None
        or not snapshot.clarify_started
        or snapshot.state is not TaskState.WAIT_INPUT
    ):
        return
    await finish(bus, snapshot, apply=None)


async def handle_consult(
    bus, snapshot: TaskSnapshot, envelope: Envelope, consult: dict | None = None
) -> None:
    """Record one peer's consult result; finish the round when all are in."""
    if not snapshot.clarify_started or snapshot.state is not TaskState.WAIT_INPUT:
        return
    if consult is None:
        consult = ((envelope.payload or {}).get("output") or {}).get("consult")
    if consult is None:
        return
    peer = envelope.role.value if envelope.role else "unknown"
    if peer not in snapshot.pending_consults:
        return
    snapshot.pending_consults.remove(peer)
    try:
        answer = ConsultAnswer.model_validate(consult)
    except Exception:  # noqa: BLE001 - a broken answer must not stall the round
        answer = ConsultAnswer(cannot_answer="invalid consult payload")

    if answer.answer:
        snapshot.peer_answers[peer] = answer.answer
        await bus.emit_event(snapshot, "clarify.peer_answer", f"{peer}: {answer.answer}")
    else:
        snapshot.peer_answers[peer] = ""
        await bus.emit_event(snapshot, "clarify.peer_declined", f"{peer}: {answer.cannot_answer}")
    await bus.put_snapshot(snapshot)

    if not snapshot.pending_consults:
        await finish(bus, snapshot, apply=None)


async def finish(bus, snapshot: TaskSnapshot, apply: ApplyFn | None) -> None:
    """Close the clarify round: resume with peer answers or leave to human.

    ``apply`` (the engine's transition function) is invoked with the
    ``answer`` event when peers answered; the caller saves the snapshot.
    """
    if not snapshot.clarify_started:
        return
    snapshot.clarify_started = False
    snapshot.clarify_round += 1
    snapshot.pending_consults = []

    answers = {peer: text for peer, text in snapshot.peer_answers.items() if text}
    question = snapshot.question
    if answers:
        compiled = "\n".join(f"- {peer}: {text}" for peer, text in answers.items())
        snapshot.task += f'\n\nPeer answers to the question "{question}":\n{compiled}'
        snapshot.question = ""
        asker = snapshot.question_role or Role.PLANNER
        await bus.put_snapshot(snapshot)
        await bus.emit_event(
            snapshot,
            "clarify.resolved",
            f"peers answered; resuming {asker.value}",
        )
        if apply is not None:
            await apply(snapshot, Event("answer", reason=compiled, role=asker))
        else:
            # No engine apply fn (deadline path): emulate the FSM handoff so
            # the asking role becomes claimable again.
            resume = {
                Role.PLANNER: TaskState.PLANNING,
                Role.CODER: TaskState.CODING,
                Role.CRITIC: TaskState.CRITIQUE,
            }[asker]
            snapshot.state = resume
            snapshot.next_role = asker.value
            snapshot.claimable = True
            snapshot.claimed_by = ""
            snapshot.attempt = 0
            await bus.put_snapshot(snapshot)
    else:
        snapshot.record("task.question", question)
        await bus.put_snapshot(snapshot)
        await bus.emit_event(
            snapshot,
            "task.question",
            f"question: {question} — peers could not answer — /answer {snapshot.task_id} <text>",
        )
