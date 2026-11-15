"""Tests for worker-side observation deposits (15.3 item 5, self_pipeline)."""

import asyncio
import json
import os

from test_m14 import SilentBus, _worker

from memnc.config import Settings
from memnc.roles import Role
from memnc.schema import TaskSnapshot, TaskState


def _snapshot() -> TaskSnapshot:
    return TaskSnapshot(
        task_id="12345678-1",
        trace_id="tr-1",
        task="write a function",
        state=TaskState.DONE,
        route={"coder": "node-a-coder:llama"},
    )


class _CaptureBus(SilentBus):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.deposited: list[object] = []

    async def deposit_observation(self, observation) -> None:
        self.deposited.append(observation)


class TestWorkerDeposit:
    def test_deposits_and_records_experience(self, tmp_path) -> None:
        path = os.path.join(tmp_path, "exp.json")
        settings = Settings(
            node_name="node-a-coder",
            experience_from_observations=True,
            experience_store_path=path,
        )
        bus = _CaptureBus(settings)
        w = _worker(Role.CODER, bus, None)
        w.settings.experience_from_observations = True
        w.settings.experience_store_path = path
        asyncio.run(w._deposit_observation(_snapshot()))
        assert len(bus.deposited) == 1
        assert bus.deposited[0].outcome == "done"
        data = json.load(open(path, encoding="utf-8"))
        assert any(b["topic"] == "success" for b in data)

    def test_flag_off_deposits_but_skips_experience(self, tmp_path) -> None:
        path = os.path.join(tmp_path, "exp.json")
        settings = Settings(node_name="node-a-coder", experience_store_path=path)
        bus = _CaptureBus(settings)
        w = _worker(Role.CODER, bus, None)
        asyncio.run(w._deposit_observation(_snapshot()))
        assert len(bus.deposited) == 1
        assert not os.path.exists(path)

    def test_deposit_failure_is_swallowed(self, tmp_path) -> None:
        settings = Settings(node_name="node-a-coder", experience_from_observations=True)

        class _BrokenBus(SilentBus):
            async def deposit_observation(self, observation) -> None:
                raise RuntimeError("kv down")

        w = _worker(Role.CODER, _BrokenBus(settings), None)
        asyncio.run(w._deposit_observation(_snapshot()))  # must not raise

    def test_bus_without_deposit_support_is_ignored(self, tmp_path) -> None:
        settings = Settings(node_name="node-a-coder", experience_from_observations=True)
        bus = SilentBus(settings)
        w = _worker(Role.CODER, bus, None)
        asyncio.run(w._deposit_observation(_snapshot()))  # no deposit_observation attr
