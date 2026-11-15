"""Tests for pheromone routing (14.15.10): scoring, seeds, KV store."""

import asyncio

from memnc.pheromone import (
    LIVE_EDGE,
    PHEROMONE_BUCKET,
    PheromoneEntry,
    PheromoneStore,
    choose_edge,
    edge_key,
    entries_by_task,
    escalate,
    format_pheromones,
    seed_entries,
)


def test_edge_key_sanitizes_illegal_chars():
    assert edge_key("hard_bst", "A") == "hard_bst.A"
    assert ":" not in edge_key("a:b", "x>y")


def test_eta_dominates_when_no_deposits():
    # cost prior only: cheaper edge wins before any evidence arrives
    cheap = PheromoneEntry(cost_tokens=500)
    dear = PheromoneEntry(cost_tokens=5000)
    assert cheap.score() > dear.score()
    assert cheap.eta() > dear.eta()


def test_success_deposits_lift_tau():
    fresh = PheromoneEntry()
    assert fresh.tau() == 1.0
    fresh.deposit("success", 100)
    assert fresh.tau() == 2.0
    fresh.deposit("unsure", 100)
    assert fresh.tau() == 3.0
    fresh.deposit("failure", 100)
    assert fresh.tau() == 3.0
    assert fresh.failures == 1


def test_rescue_matrix_beats_flat_prior():
    # rate limiter: A failed 5/5, B succeeded 4/5 -> B wins despite cost
    entries = seed_entries()
    edge = choose_edge(entries, "hard_rate_limiter")
    assert edge == "B"
    # easiest task: A at 5/5 with the lowest cost wins
    assert choose_edge(entries, "hard_matrix_rotate") == "A"


def test_failed_edge_is_never_repicked():
    entries = seed_entries()
    edge = choose_edge(entries, "hard_rate_limiter", failed_edge="B")
    assert edge != "B"
    assert edge in ("A", "C")


def test_escalate_chain():
    assert escalate("A") == "B"
    assert escalate("B") == "C"
    assert escalate("C") == "C"
    assert escalate("mesh") == "A"


def test_format_pheromones():
    text = format_pheromones(seed_entries(), task_type="hard_rate_limiter")
    assert "hard_rate_limiter.A" in text
    assert "no pheromone deposits" in format_pheromones({}, task_type="x")
    assert "no pheromone deposits" in format_pheromones({})


def test_seed_covers_all_cells():
    entries = seed_entries()
    assert len(entries) == 24
    grouped = entries_by_task(entries)
    assert set(grouped) == {
        "hard_bst",
        "hard_dijkstra",
        "hard_longest_substr",
        "hard_matrix_rotate",
        "hard_rate_limiter",
        "hard_tokenizer",
        "hard_topo_sort",
        "hard_word_ladder",
    }


class FakeEntry:
    def __init__(self, value: bytes, revision: int) -> None:
        self.value = value
        self.revision = revision


class FakeKV:
    def __init__(self) -> None:
        self.data: dict[str, tuple[bytes, int]] = {}
        self._rev = 0

    async def get(self, key: str) -> FakeEntry:
        if key not in self.data:
            from nats.js.errors import NotFoundError

            raise NotFoundError()
        value, rev = self.data[key]
        return FakeEntry(value, rev)

    async def put(self, key: str, value: bytes) -> int:
        self._rev += 1
        self.data[key] = (value, self._rev)
        return self._rev

    async def keys(self) -> list[str]:
        return sorted(self.data)


class FakeJS:
    def __init__(self, exists: bool = True) -> None:
        self.kv = FakeKV()
        self.exists = exists
        self.created: list[str] = []

    async def key_value(self, bucket: str):
        if not self.exists:
            from nats.js.errors import NotFoundError

            raise NotFoundError()
        return self.kv

    async def create_key_value(self, config):
        self.created.append(config.bucket)
        return self.kv


def test_store_ensure_creates_missing_bucket():
    async def run():
        js = FakeJS(exists=False)
        store = PheromoneStore(js)
        await store.ensure()
        assert js.created == [PHEROMONE_BUCKET]

    asyncio.run(run())


def test_store_deposit_and_get():
    async def run():
        js = FakeJS()
        store = PheromoneStore(js)
        await store.ensure()
        # bucket already exists -> no creation
        assert js.created == []
        await store.deposit("task_x", "B", "success", 1500)
        await store.deposit("task_x", "B", "failure", 900)
        entry = await store.get("task_x", "B")
        assert (entry.successes, entry.failures) == (1, 1)
        assert entry.tokens == 2400
        fresh = await store.get("task_x", "Z")
        assert fresh.tau() == 1.0

    asyncio.run(run())


def test_store_seed_skips_existing_cells():
    async def run():
        js = FakeJS()
        store = PheromoneStore(js)
        await store.ensure()
        assert await store.seed() == 24
        # second seed pass must be a no-op (cells already present)
        assert await store.seed() == 0
        entries = await store.entries()
        assert entries[edge_key("hard_rate_limiter", "B")].successes == 4

    asyncio.run(run())


def test_live_edge_constant():
    assert LIVE_EDGE == "mesh"


def test_orchestrator_terminal_deposits_pheromone():
    async def run():
        from types import SimpleNamespace

        from memnc.config import Settings
        from memnc.orchestrator import Orchestrator
        from memnc.schema import TaskState

        class Bus:
            def __init__(self):
                self.calls = []

            async def deposit_pheromone(self, task_type, edge, outcome, tokens=0):
                self.calls.append((task_type, edge, outcome, tokens))

        class NoPheromoneBus:
            pass

        orch = Orchestrator(Settings())
        bus = Bus()
        orch.bus = bus  # type: ignore[assignment]
        snap = SimpleNamespace(
            task="Write a rate limiter with sliding window", state=TaskState.DONE,
            task_id="t9",
        )
        await orch._deposit_pheromone(snap)
        assert bus.calls == [("write-a-rate-limiter-with-sliding-window", "mesh", "success", 0)]

        snap.state = TaskState.FAILED
        await orch._deposit_pheromone(snap)
        assert bus.calls[-1][2] == "failure"

        # bus without the hook: no-op, must not raise
        orch.bus = NoPheromoneBus()  # type: ignore[assignment]
        await orch._deposit_pheromone(snap)

    asyncio.run(run())


def test_task_type_slug():
    from memnc.pheromone import task_type_from_prompt

    assert task_type_from_prompt("Fix the bug: colon handling") == "fix-the-bug-colon-handling"
    assert len(task_type_from_prompt("x" * 200)) == 40
    assert task_type_from_prompt("") == "live"


def test_evaporate_fades_counters():
    entry = PheromoneEntry(successes=10, unsure=4, failures=2, tokens=1000, cost_tokens=500)
    assert entry.evaporate(0.1)
    assert (entry.successes, entry.unsure, entry.failures, entry.tokens) == (9.0, 3.6, 1.8, 900.0)
    # static cost prior is a measurement, not accumulated belief: not decayed
    assert entry.cost_tokens == 500
    # tau decays with the deposit counters
    assert entry.tau() == 1.0 + 9.0 + 3.6


def test_evaporate_noop_on_fresh_cell():
    fresh = PheromoneEntry()
    assert not fresh.evaporate(0.1)


def test_store_decay_rewrites_only_changed_cells():
    async def run():
        js = FakeJS()
        store = PheromoneStore(js)
        await store.ensure()
        await store.deposit("task_x", "B", "success", 1500)
        # empty cell already in the bucket: evaporate must skip it (no put)
        empty_key = edge_key("task_y", "A")
        await store.kv.put(empty_key, PheromoneEntry().model_dump_json().encode())
        updated = await store.decay(0.5)
        assert updated == 1
        entry = await store.get("task_x", "B")
        assert entry.successes == 0.5
        assert entry.tokens == 750.0
        # second pass at full decay: 0.5 -> 0.25, still material
        assert await store.decay(0.5) == 1

    asyncio.run(run())


def test_pheromone_loop_disabled_when_interval_zero():
    async def run():
        from memnc.config import Settings
        from memnc.orchestrator import Orchestrator

        orch = Orchestrator(Settings(pheromone_decay_interval_s=0))
        await asyncio.wait_for(asyncio.shield(orch._pheromone_loop()), 0.5)

    asyncio.run(run())


def test_pheromone_loop_calls_decay():
    async def run():
        from memnc.config import Settings
        from memnc.orchestrator import Orchestrator

        class Bus:
            def __init__(self):
                self.rhos = []

            async def decay_pheromones(self, rho):
                self.rhos.append(rho)
                return 3

        orch = Orchestrator(
            Settings(pheromone_decay_interval_s=1, pheromone_rho=0.25),
            bus=Bus(),
        )
        # pass 1 after the first interval; the second sleep completes before
        # the stop flag is re-checked, so pass 2 also runs before exit
        asyncio.get_running_loop().call_later(1.2, orch._stop.set)
        await asyncio.wait_for(orch._pheromone_loop(), 3.0)
        assert orch.bus.rhos == [0.25, 0.25]

    asyncio.run(run())
