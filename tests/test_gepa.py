"""Tests for the GEPA-style bench-driven prompt evolution (15.3 item 4)."""

from __future__ import annotations

import asyncio
import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from memnc.bench.gepa import (
    Candidate,
    FailureExample,
    evaluate_candidate,
    evolve,
    load_failures,
    mutation_user_prompt,
    pareto_front,
    propose_mutation,
)


def test_load_failures_filters_arm_and_success() -> None:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "run.jsonl"
        rows = [
            {"arm": "A", "task_id": "t1", "success": False, "issues": ["bad edge"]},
            {"arm": "A", "task_id": "t2", "success": True},
            {"arm": "B", "task_id": "t3", "success": False, "error": "boom"},
            {"arm": "A", "task_id": "t4", "success": False},
        ]
        path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
        failures = load_failures(path, "A")
    assert [f.task_id for f in failures] == ["t1", "t4"]
    assert failures[0].issues == ["bad edge"]
    assert failures[1].issues == []


def test_mutation_user_prompt_lists_traces() -> None:
    failures = [
        FailureExample(task_id="t1", issues=["a", "b"]),
        FailureExample(task_id="t2", error="boom"),
    ]
    text = mutation_user_prompt("BASE", failures)
    assert "BASE" in text
    assert "t1: a; b" in text
    assert "t2: boom" in text


def test_propose_mutation_parses_fenced_json() -> None:
    async def reflect(system: str, user: str, model: str, temp: float) -> str:
        assert "CURRENT PROMPT" in user
        return '```json\n{"prompt": "NEW", "rationale": "r"}\n```'

    out = asyncio.run(propose_mutation(reflect, "BASE", []))
    assert out == "NEW"


def test_propose_mutation_falls_back_to_base() -> None:
    async def reflect(system: str, user: str, model: str, temp: float) -> str:
        return "no json here at all"

    out = asyncio.run(propose_mutation(reflect, "BASE", []))
    assert out == "BASE"


def test_propose_mutation_tolerates_tuple_chat_shape() -> None:
    async def reflect(system: str, user: str, model: str, temp: float):
        return (None, '{"prompt": "T", "rationale": "x"}', 1, 1)

    out = asyncio.run(propose_mutation(reflect, "BASE", []))
    assert out == "T"


def test_pareto_front_prunes_dominated() -> None:
    weak = Candidate(role="coder", prompt="w", scores={"t1": False, "t2": False})
    strong = Candidate(role="coder", prompt="s", scores={"t1": True, "t2": False})
    split = Candidate(role="coder", prompt="x", scores={"t1": False, "t2": True})
    front = pareto_front([weak, strong, split])
    assert weak not in front
    assert strong in front and split in front


def test_pareto_front_ties_survive() -> None:
    a = Candidate(role="coder", prompt="a", scores={"t1": True})
    b = Candidate(role="coder", prompt="b", scores={"t1": True})
    assert a in pareto_front([a, b]) and b in pareto_front([a, b])


@dataclass
class _FakeTask:
    id: str
    prompt: str = "do it"
    tier: str = "hard"


@dataclass
class _FakeRunResult:
    success: bool = False
    llm_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    output_files: int = 0
    route: dict = field(default_factory=dict)


class _FakeEngine:
    """Stands in for BenchEngine; records the system prompt seen per run."""

    def __init__(self, succeed_for: set[str]):
        self.succeed_for = succeed_for
        self.seen_prompts: list[str] = []

    async def run_task(self, task, arm, rep, workroot) -> _FakeRunResult:
        from memnc.roles import SYSTEM_PROMPTS, Role

        self.seen_prompts.append(SYSTEM_PROMPTS[Role.CODER])
        return _FakeRunResult(success=task.id in self.succeed_for)


def test_evaluate_candidate_swaps_and_restores_prompt() -> None:
    from memnc.roles import SYSTEM_PROMPTS, Role

    engine = _FakeEngine(succeed_for={"t1"})
    original = SYSTEM_PROMPTS[Role.CODER]
    cand = Candidate(role="coder", prompt="MUTATED")
    tasks = [_FakeTask("t1"), _FakeTask("t2")]
    out = asyncio.run(evaluate_candidate(engine, "coder", cand, tasks, "A"))
    assert out.scores == {"t1": True, "t2": False}
    assert engine.seen_prompts == ["MUTATED", "MUTATED"]
    assert SYSTEM_PROMPTS[Role.CODER] == original


def test_evaluate_candidate_restores_prompt_on_engine_error() -> None:
    from memnc.roles import SYSTEM_PROMPTS, Role

    class _Boom:
        async def run_task(self, task, arm, rep, workroot):
            raise RuntimeError("model down")

    original = SYSTEM_PROMPTS[Role.CODER]
    cand = Candidate(role="coder", prompt="MUTATED")
    try:
        asyncio.run(
            evaluate_candidate(_Boom(), "coder", cand, [_FakeTask("t1")], "A")
        )
    except RuntimeError:
        pass
    assert SYSTEM_PROMPTS[Role.CODER] == original


def test_evolve_prunes_and_evaluates_generations() -> None:
    prompts: list[str] = []

    async def reflect(system: str, user: str, model: str, temp: float) -> str:
        prompts.append("P")
        return json.dumps({"prompt": f"M{len(prompts)}", "rationale": "r"})

    engine = _FakeEngine(succeed_for=set())  # everything fails -> all scores False
    tasks = [_FakeTask("t1")]
    pool = asyncio.run(
        evolve(
            engine,
            reflect,
            "coder",
            "A",
            "BASE",
            [FailureExample(task_id="t1", issues=["x"])],
            generations=2,
            eval_tasks=tasks,
        )
    )
    # All candidates score identically (all False): ties survive, so the seed
    # and both mutations are on the frontier.
    assert len(pool) == 3
    assert pool[0].prompt == "BASE"
    assert {c.gen for c in pool} == {0, 1, 2}


def test_evolve_requires_eval_tasks() -> None:
    async def reflect(system, user, model, temp):
        return '{"prompt": "X"}'

    try:
        asyncio.run(
            evolve(_FakeEngine(set()), reflect, "coder", "A", "BASE", [], 1)
        )
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for missing eval_tasks")
