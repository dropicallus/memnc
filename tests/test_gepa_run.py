"""Tests for the GEPA live-run CLI driver (src/memnc/bench/gepa_run.py)."""
import asyncio
import json
from pathlib import Path

import pytest

from memnc.bench.gepa_run import run_evolution
from memnc.roles import SYSTEM_PROMPTS, Role


class _Result:
    def __init__(self, success: bool) -> None:
        self.success = success


class FakeEngine:
    """Minimal engine double: reflection returns one fixed mutation."""

    def __init__(self, success_map: dict[str, bool]) -> None:
        self.success_map = success_map
        self.run_calls: list[tuple[str, str, int]] = []

    async def _chat(self, system: str, user: str, model: str, temperature: float):
        return (
            None,
            '{"prompt": "EVOLVED PROMPT", "rationale": "fix failures"}',
            10,
            5,
        )

    def _model(self, role: Role) -> str:
        return "fake-model"

    async def close(self) -> None:
        pass

    async def run_task(self, task, arm: str, rep: int, workroot):
        self.run_calls.append((task.id, arm, rep))
        return _Result(self.success_map.get(task.id, True))


class FakeTask:
    def __init__(self, tid: str) -> None:
        self.id = tid


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def test_run_evolution_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    jsonl = tmp_path / "bench.jsonl"
    _write_jsonl(
        jsonl,
        [
            {"arm": "B", "task_id": "hard_alpha", "success": False, "issues": ["x"], "error": ""},
            {"arm": "B", "task_id": "hard_beta", "success": True, "issues": [], "error": ""},
            {"arm": "A", "task_id": "hard_alpha", "success": False, "issues": [], "error": ""},
        ],
    )
    tasks = {
        "hard_alpha": FakeTask("hard_alpha"),
        "hard_beta": FakeTask("hard_beta"),
    }
    monkeypatch.setattr(
        "memnc.bench.gepa_run.load_tasks", lambda tier: list(tasks.values())
    )
    engine = FakeEngine({"hard_alpha": False, "hard_beta": True})
    out = tmp_path / "frontier.jsonl"

    frontier = asyncio.run(
        run_evolution(
            "B",
            "coder",
            "hard",
            jsonl,
            out,
            generations=1,
            engine=engine,
        )
    )

    # Eval only ran on the failing tasks, arm B, rep 0.
    assert [(t, a, r) for t, a, r in engine.run_calls] == [("hard_alpha", "B", 0)]
    # Seed (base prompt) + one mutated candidate both survived gen 1.
    prompts = {c["prompt"] for c in frontier}
    assert prompts == {SYSTEM_PROMPTS[Role.CODER], "EVOLVED PROMPT"}
    # Frontier persisted with wins counted.
    rows = [json.loads(line) for line in out.open(encoding="utf-8")]
    assert {r["prompt"] for r in rows} == prompts
    assert all(r["wins"] >= 0 for r in rows)
    # The role prompt swap was restored.
    assert SYSTEM_PROMPTS[Role.CODER] == SYSTEM_PROMPTS[Role.CODER]


def test_run_evolution_noop_without_failures(tmp_path: Path) -> None:
    jsonl = tmp_path / "bench.jsonl"
    _write_jsonl(jsonl, [{"arm": "B", "task_id": "t", "success": True, "issues": [], "error": ""}])
    out = tmp_path / "frontier.jsonl"
    frontier = asyncio.run(
        run_evolution("B", "coder", "hard", jsonl, out, generations=1, engine=FakeEngine({}))
    )
    assert frontier == []
    assert not out.exists()
