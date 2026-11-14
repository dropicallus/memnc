from memnc.fsm import Event, transition
from memnc.schema import Role, TaskState


def test_start_moves_to_planning_and_dispatches_planner():
    result = transition(TaskState.RECEIVED, Event("start"))
    assert result.state is TaskState.PLANNING
    assert result.actions[0].type == "dispatch"
    assert result.actions[0].role is Role.PLANNER


def test_planner_success_moves_to_coding_and_resets_attempts():
    result = transition(TaskState.PLANNING, Event("result", ok=True), attempt=2)
    assert result.state is TaskState.CODING
    assert result.actions[0].role is Role.CODER
    assert result.attempt_reset is True


def test_planner_failure_retries_then_fails():
    retry = transition(TaskState.PLANNING, Event("result", ok=False), attempt=0, max_attempts=3)
    assert retry.state is TaskState.PLANNING
    assert retry.attempt_delta == 1

    retry2 = transition(TaskState.PLANNING, Event("result", ok=False), attempt=1, max_attempts=3)
    assert retry2.attempt_delta == 1

    final = transition(TaskState.PLANNING, Event("timeout", ok=False), attempt=2, max_attempts=3)
    assert final.state is TaskState.FAILED
    assert final.actions[0].type == "escalate"


def test_coder_success_moves_to_critique():
    result = transition(TaskState.CODING, Event("result", ok=True))
    assert result.state is TaskState.CRITIQUE
    assert result.actions[0].role is Role.CRITIC


def test_critic_approval_finishes():
    result = transition(TaskState.CRITIQUE, Event("result", ok=True, approved=True))
    assert result.state is TaskState.DONE
    assert result.actions[0].type == "finish"


def test_critic_rejection_enters_revise_and_counts_revision():
    result = transition(
        TaskState.CRITIQUE,
        Event("result", ok=True, approved=False),
        revisions=0,
        max_revisions=2,
    )
    assert result.state is TaskState.REVISE
    assert result.revision_delta == 1
    assert result.actions == ()


def test_revise_start_dispatches_coder():
    result = transition(TaskState.REVISE, Event("start"))
    assert result.state is TaskState.CODING
    assert result.actions[0].role is Role.CODER
    assert result.attempt_reset is True


def test_max_revisions_fails_task():
    result = transition(
        TaskState.CRITIQUE,
        Event("result", ok=True, approved=False),
        revisions=2,
        max_revisions=2,
    )
    assert result.state is TaskState.FAILED
    assert result.actions[0].type == "escalate"


def test_critic_missing_verdict_is_retried():
    result = transition(TaskState.CRITIQUE, Event("result", ok=True, approved=None), attempt=0)
    assert result.state is TaskState.CRITIQUE
    assert result.attempt_delta == 1


def test_terminal_states_ignore_events():
    for state in (TaskState.DONE, TaskState.FAILED, TaskState.CANCELLED):
        result = transition(state, Event("result", ok=True))
        assert result.state is state
        assert result.actions == ()


def test_cancel_from_any_active_state():
    result = transition(TaskState.CODING, Event("cancel"))
    assert result.state is TaskState.CANCELLED
    assert result.actions[0].type == "cancel"


def test_unknown_event_is_noop():
    result = transition(TaskState.CODING, Event("start"))
    assert result.state is TaskState.CODING
    assert result.actions == ()


def test_ask_parks_task_and_resets_attempt():
    result = transition(TaskState.PLANNING, Event("ask", reason="which db?", role=Role.PLANNER))
    assert result.state is TaskState.WAIT_INPUT
    assert result.attempt_reset is True
    assert result.actions[0].type == "ask"
    assert result.actions[0].role is Role.PLANNER
    assert result.actions[0].reason == "which db?"


def test_ask_is_absorbed_when_already_waiting():
    result = transition(TaskState.WAIT_INPUT, Event("ask", reason="again", role=Role.CODER))
    assert result.state is TaskState.WAIT_INPUT
    assert result.actions == ()


def test_answer_resumes_to_asking_role():
    result = transition(
        TaskState.WAIT_INPUT,
        Event("answer", reason="postgres", role=Role.CODER),
    )
    assert result.state is TaskState.CODING
    assert result.actions[0].type == "dispatch"
    assert result.actions[0].role is Role.CODER
    assert result.attempt_reset is True


def test_answer_defaults_to_planner():
    result = transition(TaskState.WAIT_INPUT, Event("answer", reason="go on"))
    assert result.state is TaskState.PLANNING
    assert result.actions[0].role is Role.PLANNER


def test_parked_task_ignores_other_events():
    result = transition(TaskState.WAIT_INPUT, Event("result", ok=True))
    assert result.state is TaskState.WAIT_INPUT
    assert result.actions == ()
