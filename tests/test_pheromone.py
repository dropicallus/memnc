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
