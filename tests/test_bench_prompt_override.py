"""Tests for the bench prompt-override path (GEPA gen1 validation, 15.3.4)."""

import asyncio

import pytest

import memnc.bench.runner as runner_mod
from memnc.roles import SYSTEM_PROMPTS, Role


def test_override_swaps_prompt_inside_block():
    original = SYSTEM_PROMPTS[Role.CODER]
    with runner_mod.prompt_override("coder", "EVOLVED PROMPT"):
        assert SYSTEM_PROMPTS[Role.CODER] == "EVOLVED PROMPT"
    assert SYSTEM_PROMPTS[Role.CODER] == original


def test_override_restores_on_exception():
    original = SYSTEM_PROMPTS[Role.CODER]
    with pytest.raises(RuntimeError):
        with runner_mod.prompt_override("coder", "EVOLVED"):
            raise RuntimeError("boom")
    assert SYSTEM_PROMPTS[Role.CODER] == original


def test_override_requires_both_args():
    with pytest.raises(ValueError):
        with runner_mod.prompt_override("coder", None):
            pass
    with pytest.raises(ValueError):
        with runner_mod.prompt_override(None, "text"):
            pass


def test_override_unknown_role():
    with pytest.raises(ValueError):
        with runner_mod.prompt_override("nope", "text"):
            pass


def test_override_noop_when_both_none():
    original = SYSTEM_PROMPTS[Role.CODER]
    with runner_mod.prompt_override(None, None):
        assert SYSTEM_PROMPTS[Role.CODER] == original


def test_run_suite_applies_and_restores_override(monkeypatch, tmp_path):
    seen: list[str] = []
    original = SYSTEM_PROMPTS[Role.CODER]

    async def fake_run_suite(*args, **kwargs):
        seen.append(SYSTEM_PROMPTS[Role.CODER])
        return []

    monkeypatch.setattr(runner_mod, "_run_suite", fake_run_suite)
    results = asyncio.run(
        runner_mod.run_suite(
            arms=["B"],
            reps=1,
            tiers=None,
            out_path=tmp_path / "out.jsonl",
            report_path=None,
            prompt_role="coder",
            prompt_text="EVOLVED",
        )
    )
    assert results == []
    assert seen == ["EVOLVED"]
    assert SYSTEM_PROMPTS[Role.CODER] == original
