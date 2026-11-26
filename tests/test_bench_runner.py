"""Tests for the bench runner (roadmap 14.15.9 stage 2)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from memnc.backends import ChatResult
from memnc.bench import get_task
from memnc.bench.runner import (
    ARMS,
    BenchEngine,
    RunResult,
    build_suite,
    extract_json,
    wilson_interval,
    write_report,
)


def _coder_payload(files: dict[str, str], **extra: object) -> str:
    body = {"files": [{"path": p, "content": c} for p, c in files.items()]}
    body.update(extra)
    return json.dumps(body)


def _verdict(approved: bool, issues: list[str] | None = None) -> str:
    return json.dumps({"approved": approved, "issues": issues or []})


class FakeBackend:
    """Scripted backend: pops one response per chat call."""

    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def chat(self, messages, tools=None, model="", **options):  # noqa: ANN001
        self.calls.append({"messages": messages, "model": model, "options": options})
        content = self.responses.pop(0) if self.responses else "{}"
        return ChatResult(content=content, raw={"eval_count": 10, "prompt_eval_count": 20})

    async def list_models(self):  # noqa: ANN201
        return []

    async def close(self) -> None:
        pass


def _engine(monkeypatch: pytest.MonkeyPatch, responses: list[str]) -> BenchEngine:
    import memnc.backends as backends_mod

    fake = FakeBackend(responses)
    monkeypatch.setattr(backends_mod, "make_backend", lambda settings: fake)
    engine = BenchEngine.__new__(BenchEngine)
    engine.settings = _settings()
    engine.backend = fake
    engine.record_solutions = False
    return engine


def _settings():
    from memnc.config import Settings

    return Settings(node_name="bench-test")


def _reference_files(task_id: str) -> dict[str, str]:
    """Shim files that pass the real checker (same content write_reference writes)."""
    import tempfile

    from memnc.bench import write_reference

    with tempfile.TemporaryDirectory() as tmp:
        paths = write_reference(tmp, task_id)
        return {p.name: p.read_text(encoding="utf-8") for p in paths}


def test_extract_json_plain_and_fenced_and_prose() -> None:
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json("```json\n{\"a\": 2}\n```") == {"a": 2}
    assert extract_json("Here you go:\n{\"a\": 3}\ndone.") == {"a": 3}
    assert extract_json("") is None
    assert extract_json("no json here") is None


def test_wilson_interval_bounds() -> None:
    low, high = wilson_interval(0, 10)
    assert low == 0.0 and high < 0.35
    low, high = wilson_interval(10, 10)
    assert low > 0.7 and high == 1.0
    assert wilson_interval(5, 0) == (0.0, 0.0)


def test_build_suite_counts() -> None:
    suite = build_suite(["A", "B"], reps=2)
    assert len(suite) == 2 * 2 * 24
    assert all(spec["arm"] in ARMS for spec in suite)


def test_run_task_arm_a_success_and_failure(tmp_path: Path, monkeypatch) -> None:
    task = get_task("easy_palindrome")
    files = _reference_files(task.id)
    good = _engine(monkeypatch, [_coder_payload(files)])
    result = asyncio.run(good.run_task(task, "A", 0, tmp_path))
    assert result.success, result.issues
    assert result.llm_calls == 1
    assert result.revisions == 0
    assert result.tokens_out == 10

    bad = _engine(monkeypatch, [_coder_payload({"wrong.py": "x = 1"})])
    result = asyncio.run(bad.run_task(task, "A", 0, tmp_path))
    assert not result.success


def test_run_task_arm_b_stops_on_approval(tmp_path: Path, monkeypatch) -> None:
    task = get_task("easy_palindrome")
    files = _reference_files(task.id)
    engine = _engine(
        monkeypatch,
        [
            _coder_payload({"stale.py": "x = 1"}),  # first pass (wrong)
            _verdict(False, ["wrong file"]),        # reflection round 1
            _coder_payload(files),                  # revision
            _verdict(True),                         # reflection round 2 approves
        ],
    )
    result = asyncio.run(engine.run_task(task, "B", 1, tmp_path))
    assert result.success, result.issues
    assert result.revisions == 1
    assert result.llm_calls == 4


def test_run_task_arm_b_caps_at_three_rounds(tmp_path: Path, monkeypatch) -> None:
    task = get_task("easy_palindrome")
    files = _reference_files(task.id)
    engine = _engine(
        monkeypatch,
        [
            _coder_payload({"bad.py": "x = 1"}),
            _verdict(False, ["a"]),   # round 1
            _coder_payload({"bad.py": "x = 1"}),
            _verdict(False, ["b"]),   # round 2
            _coder_payload({"bad.py": "x = 1"}),
            _verdict(False, ["c"]),   # round 3
            _coder_payload(files),    # revision after round 3 (last chance)
        ],
    )
    result = asyncio.run(engine.run_task(task, "B", 0, tmp_path))
    assert result.success, result.issues
    assert result.revisions == 3
    assert result.llm_calls == 7


def test_run_task_arm_c_critic_loop(tmp_path: Path, monkeypatch) -> None:
    task = get_task("easy_palindrome")
    files = _reference_files(task.id)
    engine = _engine(
        monkeypatch,
        [
            json.dumps({"steps": ["step1"]}),        # planner
            _coder_payload({"bad.py": "x = 1"}),     # coder pass 1
            _verdict(False, ["broken"]),             # critic rejects
            _coder_payload(files),                   # revision 1
            _verdict(True),                          # critic approves
        ],
    )
    result = asyncio.run(engine.run_task(task, "C", 2, tmp_path))
    assert result.success, result.issues
    assert result.revisions == 1
    assert result.llm_calls == 5


def test_run_task_records_backend_error(tmp_path: Path, monkeypatch) -> None:
    class ExplodingBackend(FakeBackend):
        async def chat(self, messages, tools=None, model="", **options):  # noqa: ANN001
            raise RuntimeError("boom")

    import memnc.backends as backends_mod

    monkeypatch.setattr(backends_mod, "make_backend", lambda s: ExplodingBackend([]))
    engine = BenchEngine.__new__(BenchEngine)
    engine.settings = _settings()
    engine.backend = ExplodingBackend([])
    engine.record_solutions = False
    task = get_task("easy_palindrome")
    result = asyncio.run(engine.run_task(task, "A", 0, tmp_path))
    assert not result.success
    assert "boom" in result.error


def test_write_report_has_arm_rows(tmp_path: Path) -> None:
    results = [
        RunResult("r1", "A", "easy_palindrome", "easy", 0, True),
        RunResult("r2", "A", "easy_fizzbuzz", "easy", 1, False, issues=["x"]),
        RunResult("r3", "C", "easy_palindrome", "easy", 0, True, revisions=2),
    ]
    path = tmp_path / "bench-report.md"
    text = write_report(results, path)
    assert path.exists()
    assert "single-shot" in text and "mesh" in text
    assert "95% CI" in text

# -- arm D: adversarial critic (docs/roadmap.md D1) -------------------


def test_arms_include_d() -> None:
    assert "D" in ARMS
    assert build_suite(["D"], 1, ["easy"])


def test_adversarial_verdict_evidence_gate() -> None:
    from memnc.bench.runner import (
        adversarial_verdict_is_evidenced,
        normalize_adversarial_verdict,
    )

    # unevidenced approval is the polite-agreement failure mode -> rejected
    bare = {"approved": True, "issues": []}
    assert not adversarial_verdict_is_evidenced(bare)
    out = normalize_adversarial_verdict(bare)
    assert out["approved"] is False
    assert "approval without counterexample attempt" in out["issues"][0]

    # counterexample counts as evidence
    assert adversarial_verdict_is_evidenced(
        {"approved": True, "counterexample": "empty list input", "tried": []}
    )
    # tried-list counts as evidence
    assert adversarial_verdict_is_evidenced(
        {"approved": True, "tried": ["negative n", "unicode input"]}
    )
    # rejections pass through unchanged
    rej = {"approved": False, "issues": ["broken"]}
    assert normalize_adversarial_verdict(rej) == rej


def test_run_task_arm_d_approves_with_evidence(tmp_path: Path, monkeypatch) -> None:
    task = get_task("easy_palindrome")
    files = _reference_files(task.id)
    evidenced = json.dumps(
        {"approved": True, "counterexample": None, "tried": ["empty string"], "issues": []}
    )
    engine = _engine(
        monkeypatch,
        [
            json.dumps({"steps": ["step1"]}),        # planner
            _coder_payload(files),                    # coder pass 1
            evidenced,                                # adversarial critic approves
        ],
    )
    result = asyncio.run(engine.run_task(task, "D", 0, tmp_path))
    assert result.success, result.issues
    assert result.revisions == 0
    assert result.llm_calls == 3


def test_run_task_arm_d_rejects_unevidenced_approval(tmp_path: Path, monkeypatch) -> None:
    """A polite bare approval must NOT end the loop — revision follows."""
    task = get_task("easy_palindrome")
    files = _reference_files(task.id)
    bare_approval = _verdict(True)
    evidenced = json.dumps(
        {"approved": True, "tried": ["zero", "negative"], "issues": []}
    )
    engine = _engine(
        monkeypatch,
        [
            json.dumps({"steps": ["step1"]}),        # planner
            _coder_payload(files),                    # coder pass 1
            bare_approval,                            # unevidenced -> forced reject
            _coder_payload(files),                    # revision 1
            evidenced,                                # evidenced approval ends loop
        ],
    )
    result = asyncio.run(engine.run_task(task, "D", 0, tmp_path))
    assert result.success, result.issues
    assert result.revisions == 1
    assert result.llm_calls == 5


def test_run_task_arm_d_caps_at_two_rounds(tmp_path: Path, monkeypatch) -> None:
    task = get_task("easy_palindrome")
    files = _reference_files(task.id)
    rejection = _verdict(False, ["broken"])
    engine = _engine(
        monkeypatch,
        [
            json.dumps({"steps": ["step1"]}),   # planner
            _coder_payload({"bad.py": "x = 1"}),  # pass 1
            rejection,                          # round 1 reject
            _coder_payload(files),
            rejection,                          # round 2 reject
            _coder_payload(files),              # revision 2 (last, no re-review)
        ],
    )
    result = asyncio.run(engine.run_task(task, "D", 0, tmp_path))
    # capped: 2 revisions, then the loop ends without another critique call
    assert result.revisions == 2
    assert result.llm_calls == 6
