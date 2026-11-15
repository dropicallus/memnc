"""Tests for the bench DPO pair generator (roadmap 15.3 item 6)."""

from __future__ import annotations

import json
import tempfile
from dataclasses import asdict
from pathlib import Path

from memnc.bench.dpo import build_pairs, load_rows, write_pairs
from memnc.bench.runner import RunResult


def _row(task_id, run_id, success, tokens, files, tier="hard", arm="A"):
    return {
        "task_id": task_id,
        "run_id": run_id,
        "success": success,
        "tier": tier,
        "arm": arm,
        "tokens_in": tokens // 2,
        "tokens_out": tokens - tokens // 2,
        "solutions": files,
    }


def test_build_pairs_prefers_cheapest_success_and_costliest_failure() -> None:
    rows = [
        _row("t1", "t1__A0", True, 100, {"m.py": "cheap"}),
        _row("t1", "t1__A1", True, 500, {"m.py": "expensive-correct"}),
        _row("t1", "t1__B0", False, 300, {"m.py": "cheap-fail"}),
        _row("t1", "t1__B1", False, 900, {"m.py": "costly-fail"}),
    ]
    pairs = build_pairs(rows, prompts={"t1": "do the thing"})
    assert len(pairs) == 1
    pair = pairs[0]
    assert pair["chosen"] == {"m.py": "cheap"}
    assert pair["rejected"] == {"m.py": "costly-fail"}
    assert pair["chosen_run_id"] == "t1__A0"
    assert pair["rejected_run_id"] == "t1__B1"
    assert pair["prompt"] == "do the thing"
    assert pair["tier"] == "hard"


def test_build_pairs_skips_tasks_without_both_labels() -> None:
    rows = [
        _row("t1", "t1__A0", True, 100, {"m.py": "ok"}),
        _row("t2", "t2__A0", False, 100, {"m.py": "bad"}),
    ]
    assert build_pairs(rows) == []


def test_build_pairs_ignores_rows_without_solutions() -> None:
    rows = [
        {"task_id": "t1", "run_id": "r1", "success": True, "solutions": None},
        _row("t1", "r2", False, 10, {"m.py": "bad"}),
    ]
    assert build_pairs(rows) == []


def test_build_pairs_deterministic() -> None:
    rows = [
        _row("t1", "t1__A0", True, 100, {"m.py": "a"}),
        _row("t1", "t1__B0", False, 200, {"m.py": "b"}),
        _row("t2", "t2__A0", True, 100, {"m.py": "c"}),
        _row("t2", "t2__C0", False, 300, {"m.py": "d"}),
    ]
    assert build_pairs(rows) == build_pairs(list(reversed(rows)))
    assert [p["task_id"] for p in build_pairs(rows)] == ["t1", "t2"]


def test_write_pairs_jsonl_roundtrip() -> None:
    rows = [
        _row("t1", "t1__A0", True, 100, {"m.py": "a"}),
        _row("t1", "t1__B0", False, 200, {"m.py": "b"}),
    ]
    pairs = build_pairs(rows)
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "pairs.jsonl"
        write_pairs(pairs, path)
        assert load_rows(path) == pairs
        lines = path.read_text(encoding="utf-8").strip().split("\n")
        assert all(json.loads(line) for line in lines)


def test_run_result_serializes_solutions() -> None:
    result = RunResult(
        run_id="t1__A0", arm="A", task_id="t1", tier="hard", rep=0,
        solutions={"m.py": "code"},
    )
    row = asdict(result)
    assert row["solutions"] == {"m.py": "code"}
    plain = RunResult(run_id="x", arm="A", task_id="t", tier="hard", rep=0)
    assert asdict(plain)["solutions"] is None
