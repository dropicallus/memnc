"""Pure state-machine logic for the orchestrator.

No I/O, no NATS, no models: everything here is a deterministic function of
the current state plus an event, which makes the workflow trivially testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .schema import TERMINAL_STATES, Role, TaskState

EventType = Literal["start", "result", "failure", "timeout", "cancel", "human_decision"]
ActionType = Literal["dispatch", "finish", "escalate", "cancel", "schedule", "park"]


@dataclass(frozen=True)
class Event:
    type: EventType
    ok: bool = True
    approved: bool | None = None
    reason: str = ""
    result_id: str | None = None
    # DAG scheduler hints. ``all_steps_done`` advances CODING -> CRITIQUE;
    # ``exhausted`` stops re-auctioning a failed step and fails the task.
    all_steps_done: bool = False
    exhausted: bool = False


@dataclass(frozen=True)
class Action:
    type: ActionType
    role: Role | None = None
    reason: str = ""


@dataclass(frozen=True)
class Transition:
    state: TaskState
    actions: tuple[Action, ...] = ()
    attempt_delta: int = 0
    attempt_reset: bool = False
    revision_delta: int = 0


def _retry_or_fail(
    state: TaskState,
    role: Role,
    attempt: int,
    max_attempts: int,
    reason: str,
) -> Transition:
    if attempt + 1 < max_attempts:
        return Transition(
            state,
            (Action("dispatch", role, reason="retry"),),
            attempt_delta=1,
        )
    return Transition(
        TaskState.FAILED,
        (Action("escalate", reason=reason or f"{role.value} exhausted retries"),),
    )


def transition(
    state: TaskState,
    event: Event,
    *,
    attempt: int = 0,
    revisions: int = 0,
    max_attempts: int = 3,
    max_revisions: int = 3,
    self_assign: bool = False,
    gate: str = "never",
) -> Transition:
    """Compute the next state and the actions the runtime must perform."""

    if state in TERMINAL_STATES:
        return Transition(state)

    if event.type == "cancel":
        return Transition(
            TaskState.CANCELLED,
            (Action("cancel", reason=event.reason or "cancelled"),),
        )

    if state is TaskState.RECEIVED:
        if event.type == "start":
            return Transition(TaskState.PLANNING, (Action("dispatch", Role.PLANNER),))
        return Transition(state)

    if state is TaskState.PLANNING:
        if event.type == "result" and event.ok:
            if self_assign:
                return Transition(
                    TaskState.CODING,
                    (Action("schedule", reason="dag"),),
                    attempt_reset=True,
                )
            return Transition(
                TaskState.CODING,
                (Action("dispatch", Role.CODER),),
                attempt_reset=True,
            )
        if event.type in ("result", "failure", "timeout"):
            return _retry_or_fail(
                TaskState.PLANNING,
                Role.PLANNER,
                attempt,
                max_attempts,
                event.reason,
            )

    if state is TaskState.CODING:
        if self_assign:
            if event.type == "result" and event.ok:
                if event.all_steps_done:
                    return Transition(
                        TaskState.CRITIQUE,
                        (Action("dispatch", Role.CRITIC),),
                        attempt_reset=True,
                    )
                return Transition(TaskState.CODING, (Action("schedule"),))
            if event.type in ("result", "failure", "timeout"):
                if event.exhausted:
                    return Transition(
                        TaskState.FAILED,
                        (Action("escalate", reason=event.reason or "step failed"),),
                    )
                return Transition(TaskState.CODING, (Action("schedule"),))
        if event.type == "result" and event.ok:
            return Transition(
                TaskState.CRITIQUE,
                (Action("dispatch", Role.CRITIC),),
                attempt_reset=True,
            )
        if event.type in ("result", "failure", "timeout"):
            return _retry_or_fail(
                TaskState.CODING,
                Role.CODER,
                attempt,
                max_attempts,
                event.reason,
            )

    if state is TaskState.REVISE:
        if event.type == "start":
            if self_assign:
                return Transition(
                    TaskState.CODING,
                    (Action("schedule", reason="revision"),),
                    attempt_reset=True,
                )
            return Transition(
                TaskState.CODING,
                (Action("dispatch", Role.CODER),),
                attempt_reset=True,
            )

    if state is TaskState.CRITIQUE:
        # T3: human decision from the parked approval gate.
        if event.type == "human_decision":
            if event.approved:
                return Transition(TaskState.DONE, (Action("finish"),))
            if revisions < max_revisions:
                return Transition(TaskState.REVISE, revision_delta=1, attempt_reset=True)
            return Transition(
                TaskState.FAILED,
                (Action("escalate", reason="rejected at gate, max revisions reached"),),
            )
        if event.type == "result" and event.ok:
            approved = event.approved
            if approved is True:
                # T3 gate "always": even an approval parks for the human.
                if gate == "always":
                    return Transition(
                        TaskState.CRITIQUE, (Action("park", reason="gate always"),)
                    )
                return Transition(TaskState.DONE, (Action("finish"),))
            if approved is False:
                # T3 gate "always"/"fail": a rejection parks instead of
                # auto-revising, so the human sees the verdict first.
                if gate in ("always", "fail"):
                    return Transition(
                        TaskState.CRITIQUE, (Action("park", reason="critic rejected"),)
                    )
                if revisions < max_revisions:
                    return Transition(
                        TaskState.REVISE,
                        revision_delta=1,
                        attempt_reset=True,
                    )
                return Transition(
                    TaskState.FAILED,
                    (Action("escalate", reason="max revisions reached"),),
                )
            return _retry_or_fail(
                TaskState.CRITIQUE,
                Role.CRITIC,
                attempt,
                max_attempts,
                "critic returned no verdict",
            )
        if event.type in ("result", "failure", "timeout"):
            return _retry_or_fail(
                TaskState.CRITIQUE,
                Role.CRITIC,
                attempt,
                max_attempts,
                event.reason,
            )

    return Transition(state)
