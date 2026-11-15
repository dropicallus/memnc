"""Tests for pheromone strategy consumption (roadmap 14.15.10)."""

import asyncio

import memnc.bench.runner as runner_mod
from memnc.bench import load_tasks
from memnc.bench.runner import BenchEngine, RunResult
from memnc.pheromone import (
    PLANNER_CAPACITY_PENALTY,
    PheromoneEntry,
    edge_key,
    route_strategy,
    seed_entries,
)


def _entry(successes: int = 0, failures: int = 0, tokens: int = 0) -> PheromoneEntry:
    e = PheromoneEntry()
    for _ in range(successes):
        e.deposit("success", 0)
    for _ in range(failures):
        e.deposit("failure", 0)
    e.cost_tokens = tokens
    return e


def _hard_task():
    tasks = load_tasks()
    return next(t for t in tasks if t.id == "hard_rate_limiter")


class TestRouteStrategy:
    def test_seed_field_routes_rate_limiter_to_self_reflect(self):
        # Seed: A 0/5 @572 tok, B 4/5 @1963, C 1/5 @3830 (planner-penalized).
        assert route_strategy(seed_entries(), "hard_rate_limiter") == "B"

    def test_seed_field_routes_cheap_saturated_task_to_single_shot(self):
        # hard_bst: A 5/5 @741 beats B 5/5 @1649 and the penalized C.
        assert route_strategy(seed_entries(), "hard_bst") == "A"

    def test_failure_raises_the_floor(self):
        # After A failed on hard_bst, A is excluded; B still beats C.
        assert route_strategy(seed_entries(), "hard_bst", failed_edge="A") == "B"

    def test_repeated_failure_escalates_to_chain_end(self):
        # rate_limiter: B failed -> only C remains on/above the floor.
        assert route_strategy(seed_entries(), "hard_rate_limiter", failed_edge="B") == "C"

    def test_chain_end_failure_stays_at_the_end(self):
        # C failed: no candidate remains; routing stays at the chain end.
        assert route_strategy(seed_entries(), "hard_rate_limiter", failed_edge="C") == "C"

    def test_non_chain_failed_edge_resets_floor_to_head(self):
        # Live edge "mesh" is not on the bench chain -> floor is the head.
        assert route_strategy(seed_entries(), "hard_bst", failed_edge="mesh") == "A"

    def test_unknown_task_type_falls_back_to_chain_head(self):
        assert route_strategy(seed_entries(), "hard_unknown_type") == "A"

    def test_planner_penalty_discounts_mesh_edge(self):
        entries = {
            edge_key("t", "B"): _entry(successes=5, tokens=1600),
            edge_key("t", "C"): _entry(successes=5, tokens=1000),
        }
        # C is cheaper raw but pays the planner-capacity penalty.
        assert route_strategy(entries, "t", weak_planner=True) == "B"
        # With a strong planner registered, the raw winner wins.
        assert route_strategy(entries, "t", weak_planner=False) == "C"
        assert PLANNER_CAPACITY_PENALTY < 1.0


def _bare_engine() -> BenchEngine:
    eng = BenchEngine.__new__(BenchEngine)
    eng.settings = None
    eng.record_solutions = False
    eng.backend = None
    eng._phero_field = None
    return eng


class TestArmP:
    def test_arm_p_routes_escalates_and_deposits(self, tmp_path, monkeypatch):
        eng = _bare_engine()
        calls: list[str] = []

        async def fake_produce(task, arm, temperature, result, workdir):
            calls.append(arm)
            return {"solution.py": "x"} if len(calls) >= 2 else {}

        def fake_verify(task, workdir):
            return [] if len(calls) >= 2 else ["checker failed"]

        monkeypatch.setattr(eng, "_produce_files", fake_produce)
        monkeypatch.setattr(runner_mod, "verify_task", fake_verify)

        row = asyncio.run(eng.run_task(_hard_task(), "P", 0, tmp_path))
        # Seed routes rate_limiter to B first; B fails -> floor escalates to C.
        assert row.routed_edges == ["B", "C"]
        assert row.success is True
        assert calls == ["B", "C"]
        field = eng._phero_field
        assert field[edge_key("hard_rate_limiter", "B")].failures == 2.0  # seed 1 + 1
        assert field[edge_key("hard_rate_limiter", "C")].successes == 2.0  # seed 1 + 1

    def test_arm_p_succeeds_on_first_choice(self, tmp_path, monkeypatch):
        eng = _bare_engine()
        calls: list[str] = []

        async def fake_produce(task, arm, temperature, result, workdir):
            calls.append(arm)
            return {"solution.py": "x"}

        monkeypatch.setattr(eng, "_produce_files", fake_produce)
        monkeypatch.setattr(runner_mod, "verify_task", lambda t, w: [])

        row = asyncio.run(eng.run_task(_hard_task(), "P", 1, tmp_path))
        assert row.routed_edges == ["B"]
        assert row.success is True
        assert calls == ["B"]

    def test_arm_p_all_attempts_fail(self, tmp_path, monkeypatch):
        eng = _bare_engine()

        async def fake_produce(task, arm, temperature, result, workdir):
            return {}

        monkeypatch.setattr(eng, "_produce_files", fake_produce)
        monkeypatch.setattr(runner_mod, "verify_task", lambda t, w: ["still broken"])

        row = asyncio.run(eng.run_task(_hard_task(), "P", 2, tmp_path))
        # B fails -> C fails (chain end) -> stop; no exception leaks as error.
        assert row.routed_edges == ["B", "C"]
        assert row.success is False
        assert row.error == ""

    def test_arm_p_produce_exception_deposits_failure(self, tmp_path, monkeypatch):
        eng = _bare_engine()
        state = {"n": 0}

        async def fake_produce(task, arm, temperature, result, workdir):
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("backend down")
            return {"solution.py": "x"}

        monkeypatch.setattr(eng, "_produce_files", fake_produce)
        monkeypatch.setattr(runner_mod, "verify_task", lambda t, w: [])

        row = asyncio.run(eng.run_task(_hard_task(), "P", 3, tmp_path))
        assert row.routed_edges == ["B", "C"]
        assert row.success is True
        assert eng._phero_field[edge_key("hard_rate_limiter", "B")].failures == 2.0

    def test_fixed_arms_do_not_touch_routed_edges(self, tmp_path, monkeypatch):
        eng = _bare_engine()

        async def fake_produce(task, arm, temperature, result, workdir):
            workdir.mkdir(parents=True, exist_ok=True)
            return {"solution.py": "x"}

        monkeypatch.setattr(eng, "_produce_files", fake_produce)
        monkeypatch.setattr(runner_mod, "verify_task", lambda t, w: [])

        row = asyncio.run(eng.run_task(_hard_task(), "A", 0, tmp_path))
        assert isinstance(row, RunResult)
        assert row.routed_edges == []
        assert row.success is True


class TestReport:
    def test_report_prints_routed_distribution(self, tmp_path):
        from memnc.bench.runner import ARM_NAMES, RunResult, write_report

        rows = [
            RunResult(run_id="t__P0", arm="P", task_id="t", tier="hard", rep=0,
                      routed_edges=["B", "C"]),
            RunResult(run_id="t__P1", arm="P", task_id="t", tier="hard", rep=1,
                      routed_edges=["B"]),
        ]
        text = write_report(rows, tmp_path / "r.md")
        assert "arm P routed attempts (mean 1.50/run)" in text
        assert "B: 2, C: 1" in text
        assert ARM_NAMES["P"] == "pheromone-routed"
