"""Tests for the sync bench tier (roadmap 15.3 item 7, SyncMind)."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from test_bench_runner import _coder_payload, _engine

from memnc.bench import load_tasks, verify_task, write_reference


def test_sync_tasks_have_amendments_and_checkers() -> None:
    tasks = load_tasks("sync")
    assert len(tasks) == 4
    for task in tasks:
        assert task.tier == "sync"
        assert task.amendment, task.id
        assert task.checker.startswith("check_sync_")
    # Other tiers have no amendments.
    for task in load_tasks("easy") + load_tasks("medium") + load_tasks("hard"):
        assert task.amendment == "", task.id


def test_reference_solutions_pass_sync_checkers() -> None:
    with tempfile.TemporaryDirectory() as d:
        for task in load_tasks("sync"):
            write_reference(d, task.id)
            assert verify_task(task, Path(d)) == [], task.id


def test_base_spec_solution_fails_sync_checker() -> None:
    # The pre-amendment spec (no total_sent cap) must FAIL the checker.
    with tempfile.TemporaryDirectory() as d:
        Path(d, "sol_rate_limit.py").write_text(
            "def can_send(sent_today: int, limit: int) -> bool:\n"
            "    return sent_today < limit\n",
            encoding="utf-8",
        )
        task = load_tasks("sync")[0]
        assert task.id == "sync_rate_limit"
        assert verify_task(task, Path(d)) != []


def test_run_task_arm_a_applies_amendment(monkeypatch) -> None:
    task = load_tasks("sync")[0]
    assert task.amendment
    base = _coder_payload({"sol_rate_limit.py": "def can_send(a, b):\n    return a < b\n"})
    amended = _coder_payload(
        {
            "sol_rate_limit.py": (
                "def can_send(sent_today, limit, total_sent):\n"
                "    return sent_today < limit and total_sent < 10\n"
            )
        }
    )
    engine = _engine(monkeypatch, [base, amended])
    with tempfile.TemporaryDirectory() as d:
        result = asyncio.run(engine.run_task(task, "A", 0, Path(d)))
    assert result.success, result.issues
    assert result.llm_calls == 2  # first pass + amendment pass
    # The amendment context reached the coder on the second call.
    second = engine.backend.calls[1]["messages"]
    assert "ENVIRONMENT UPDATE" in second[1]["content"]
    assert task.amendment[:40] in second[1]["content"]


def test_run_task_arm_a_fails_when_amendment_ignored(monkeypatch) -> None:
    task = load_tasks("sync")[0]
    base = _coder_payload({"sol_rate_limit.py": "def can_send(a, b):\n    return a < b\n"})
    # The "amended" turn returns the same base-spec code -> checker fails.
    engine = _engine(monkeypatch, [base, base])
    with tempfile.TemporaryDirectory() as d:
        result = asyncio.run(engine.run_task(task, "A", 0, Path(d)))
    assert not result.success
    assert result.issues


def test_amendment_helper_is_noop_without_amendment(monkeypatch) -> None:
    task = load_tasks("easy")[0]
    engine = _engine(monkeypatch, [])
    files = {"sol_palindrome.py": "code"}
    result = asyncio.run(
        engine._apply_amendment(task, files, 0.0, type("R", (), {"llm_calls": 0})())
    )
    assert result == files
