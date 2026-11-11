"""Tests for the observation deposit schema (plan section 14.12, step 1)."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from test_control import FakeBus

from memnc.bus import Bus
from memnc.config import Settings
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


# -- 14.13 step 1: deposit wiring (bus + orchestrator) -----------------------


def _bus_settings() -> Settings:
    return Settings()


def _terminal_snapshot(route: dict[str, str]) -> TaskSnapshot:
    snap = _snapshot(TaskState.DONE, route)
    snap.origin_node = "node-a"
    return snap


class TestBusDeposit:
    @pytest.mark.asyncio
    async def test_deposit_and_list_roundtrip(self) -> None:
        from test_trust_fake import FakeKV


        bus = Bus(_bus_settings())
        bus.js = MagicMock()
        bus._observations = FakeKV()
        obs = TaskObservation(
            task_id="t9",
            trace_id="tr9",
            task="x",
            outcome="done",
            route={"planner": "n:llama", "critic": "n:gemma"},
        )
        await bus.deposit_observation(obs)
        listed = await bus.list_observations()
        assert [o.task_id for o in listed] == ["t9"]
        assert listed[0].synergy_pairs() == ["gemma|+|llama"]

    @pytest.mark.asyncio
    async def test_deposit_creates_bucket_once(self) -> None:
        from test_trust_fake import FakeKV


        bus = Bus(_bus_settings())
        bus.js = MagicMock()
        bus._observations = None
        # js.key_value raises NotFoundError -> create_key_value returns the fake
        bus.js.key_value = AsyncMock(
            side_effect=Exception("bucket missing")
        )
        bus.js.create_key_value = AsyncMock(return_value=FakeKV())
        # NotFoundError is what nats raises; emulate with the real error type
        from nats.js.errors import NotFoundError

        bus.js.key_value = AsyncMock(side_effect=NotFoundError)
        obs = TaskObservation(
            task_id="t1", trace_id="tr1", task="x", outcome="done"
        )
        await bus.deposit_observation(obs)
        bus.js.create_key_value.assert_awaited_once()
        assert bus._observations is not None
        assert bus._observations._store == {obs.kv_id(): obs.model_dump_json().encode()}


class _RecordingBus:
    """Fake bus that supports deposits (mimics the real Bus)."""

    def __init__(self, fail: bool = False):
        self.deposited: list[TaskObservation] = []
        self.fail = fail
        self.snapshots: dict[str, TaskSnapshot] = {}

    async def put_snapshot(self, snapshot: TaskSnapshot) -> None:
        self.snapshots[snapshot.task_id] = snapshot

    async def emit_event(self, snapshot: TaskSnapshot, event: str, detail: str = "") -> None:
        pass

    async def publish_envelope(self, subject: str, envelope) -> None:
        pass

    async def deposit_observation(self, observation: TaskObservation) -> None:
        if self.fail:
            raise RuntimeError("kv down")
        self.deposited.append(observation)


class TestOrchestratorDeposit:
    @pytest.mark.asyncio
    async def test_terminal_transition_deposits_observation(self) -> None:
        from memnc.orchestrator import Orchestrator

        orch = Orchestrator(Settings())
        bus = _RecordingBus()
        orch.bus = bus  # type: ignore[assignment]
        snap = _terminal_snapshot({"planner": "n:llama", "coder": "n:apex"})
        snap.state = TaskState.DONE
        await orch._deposit_observation(snap)
        assert len(bus.deposited) == 1
        assert bus.deposited[0].task_id == "t1"
        assert bus.deposited[0].model_names() == ["apex", "llama"]

    @pytest.mark.asyncio
    async def test_deposit_failure_is_swallowed(self) -> None:
        from memnc.orchestrator import Orchestrator

        orch = Orchestrator(Settings())
        orch.bus = _RecordingBus(fail=True)  # type: ignore[assignment]
        snap = _terminal_snapshot({"coder": "n:llama"})
        snap.state = TaskState.DONE
        # must not raise
        await orch._deposit_observation(snap)

    @pytest.mark.asyncio
    async def test_non_terminal_snapshot_deposits_nothing(self) -> None:
        from memnc.orchestrator import Orchestrator

        orch = Orchestrator(Settings())
        bus = _RecordingBus()
        orch.bus = bus  # type: ignore[assignment]
        snap = _snapshot(TaskState.PLANNING, {"planner": "n:llama"})
        await orch._deposit_observation(snap)
        assert bus.deposited == []

    @pytest.mark.asyncio
    async def test_hook_fires_on_apply_terminal(self) -> None:
        from memnc.fsm import Event
        from memnc.orchestrator import Orchestrator

        orch = Orchestrator(Settings())
        bus = _RecordingBus()
        orch.bus = bus  # type: ignore[assignment]
        snap = _terminal_snapshot({"coder": "n:llama"})
        snap.state = TaskState.CRITIQUE
        snap.critique = CriticOutput(approved=True, summary="ok")
        await orch._apply(snap, Event("result", ok=True, approved=True))
        assert len(bus.deposited) == 1
        assert bus.deposited[0].outcome == "done"

    @pytest.mark.asyncio
    async def test_hook_skipped_for_legacy_fake_buses(self) -> None:
        from memnc.fsm import Event
        from memnc.orchestrator import Orchestrator

        orch = Orchestrator(Settings())
        # Plain FakeBus without deposit_observation: must not crash.
        orch.bus = FakeBus()
        snap = _terminal_snapshot({"coder": "n:llama"})
        snap.state = TaskState.CRITIQUE
        snap.critique = CriticOutput(approved=True, summary="ok")
        await orch._apply(snap, Event("result", ok=True, approved=True))
        assert orch.bus.snapshots["t1"].state is TaskState.DONE
