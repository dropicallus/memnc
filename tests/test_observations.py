"""Tests for the observation deposit schema (plan section 14.12, step 1)."""

import pytest

from memnc.observations import (
    OBSERVATION_SCHEMA_VERSION,
    TaskObservation,
    observation_from_snapshot,
)
from memnc.schema import CriticOutput, TaskSnapshot, TaskState


def _snapshot(state: TaskState, route: dict[str, str]) -> TaskSnapshot:
    return TaskSnapshot(
        task_id="t1",
        trace_id="tr1",
        task="write a function",
        state=state,
        route=route,
    )


class TestModelNames:
    def test_distinct_sorted(self) -> None:
        obs = TaskObservation(
            task_id="t1",
            trace_id="tr1",
            task="x",
            outcome="done",
            route={"planner": "node-a:llama3.2:3b", "coder": "node-b:apex-35b"},
        )
        assert obs.model_names() == ["apex-35b", "llama3.2:3b"]

    def test_route_without_colon_is_ignored(self) -> None:
        obs = TaskObservation(
            task_id="t1",
            trace_id="tr1",
            task="x",
            outcome="done",
            route={"planner": "unrouted"},
        )
        assert obs.model_names() == []


class TestSynergyPairs:
    def test_two_models_one_pair(self) -> None:
        obs = TaskObservation(
            task_id="t1",
            trace_id="tr1",
            task="x",
            outcome="done",
            route={"planner": "n:llama", "critic": "n:gemma"},
        )
        assert obs.synergy_pairs() == ["gemma|+|llama"]

    def test_three_models_three_pairs(self) -> None:
        obs = TaskObservation(
            task_id="t1",
            trace_id="tr1",
            task="x",
            outcome="done",
            route={"planner": "n:llama", "coder": "n:apex", "critic": "n:gemma"},
        )
        assert obs.synergy_pairs() == ["apex|+|gemma", "apex|+|llama", "gemma|+|llama"]

    def test_same_model_on_two_roles_no_self_edge(self) -> None:
        obs = TaskObservation(
            task_id="t1",
            trace_id="tr1",
            task="x",
            outcome="done",
            route={"planner": "n:llama3.2:3b", "coder": "n:llama3.2:3b"},
        )
        assert obs.model_names() == ["llama3.2:3b"]
        assert obs.synergy_pairs() == []


class TestKvId:
    def test_charset_safe(self) -> None:
        import re

        obs = TaskObservation(
            task_id="abc123",
            trace_id="tr1",
            task="x",
            outcome="done",
        )
        assert re.fullmatch(r"[-_./a-zA-Z0-9]+", obs.kv_id())
        assert obs.kv_id() == f"obs.abc123.{OBSERVATION_SCHEMA_VERSION}"


class TestObservationFromSnapshot:
    def test_done_task_with_route(self) -> None:
        snap = _snapshot(TaskState.DONE, {"planner": "n:llama", "coder": "n:apex"})
        snap.critique = CriticOutput(approved=True, summary="fine")
        obs = observation_from_snapshot(snap)
        assert obs is not None
        assert obs.outcome == "done"
        assert obs.critic_verdict == "approved"
        assert obs.critique_summary == "fine"
        assert obs.synergy_pairs() == ["apex|+|llama"]

    def test_failed_task_keeps_error(self) -> None:
        snap = _snapshot(TaskState.FAILED, {"coder": "n:llama"})
        snap.error = "invalid coder output: no JSON object found"
        obs = observation_from_snapshot(snap)
        assert obs is not None
        assert obs.outcome == "failed"
        assert obs.error == "invalid coder output: no JSON object found"
        assert obs.critic_verdict is None

    def test_non_terminal_state_returns_none(self) -> None:
        assert (
            observation_from_snapshot(
                _snapshot(TaskState.PLANNING, {"planner": "n:llama"})
            )
            is None
        )

    def test_no_route_returns_none(self) -> None:
        assert observation_from_snapshot(_snapshot(TaskState.DONE, {})) is None

    def test_schema_version_stamp(self) -> None:
        obs = observation_from_snapshot(
            _snapshot(TaskState.DONE, {"coder": "n:llama"})
        )
        assert obs is not None
        assert obs.schema_version == OBSERVATION_SCHEMA_VERSION

    @pytest.mark.parametrize(
        "route, expected",
        [
            ({"planner": "n:a", "coder": "n:b", "critic": "n:c"}, 3),
            ({"planner": "n:a", "coder": "n:a"}, 0),
        ],
    )
    def test_pair_count(self, route: dict[str, str], expected: int) -> None:
        obs = observation_from_snapshot(_snapshot(TaskState.DONE, route))
        assert obs is not None
        assert len(obs.synergy_pairs()) == expected
