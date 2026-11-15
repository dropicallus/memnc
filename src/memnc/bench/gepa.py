"""GEPA-style bench-driven prompt evolution (roadmap 15.3 item 4).

DESIGN (before code, per repo rule). Evidence: GEPA / Genetic-Pareto
(Agrawal et al., ICLR 2026) — reflective prompt mutation from ~10% of
rollouts beats RL with up to 78x fewer rollouts; Pareto-based candidate
selection escapes local optima. memnc reading (docs/experience.md
2026-11-14): arm B's self-reflection used as an OFFLINE optimizer over a
bench history. All ingredients exist: bench JSONL traces, deterministic
checkers as the feedback function, modular SYSTEM_PROMPTS in roles.py.

Mechanism
---------
1. Failure harvest: read bench JSONL, collect failed rows for a role's
   arm with their checker issues (the textual feedback GEPA reflects on).
2. Reflective mutation: a reflection LM sees the base prompt + failure
   examples (task id, checker issues) and proposes a targeted prompt
   update — implicit per-module credit assignment, no weight changes.
3. Evaluation: each candidate prompt is scored by re-running the bench
   arm with ONLY that role's prompt overridden (roles.SYSTEM_PROMPTS is
   swapped in-process for the evaluation, then restored). The checker is
   the fitness function; it is never shown to the models (leakage rule
   unchanged from the bench runner).
4. Pareto selection: a candidate stays in the pool when it is the best
   on at least one task; strictly dominated candidates are pruned.
5. System-aware crossover: the reflection LM merges complementary
   lessons from two frontier candidates into one new prompt.

Cost caveat: every evaluation is a real bench rollout set (model calls).
The evolve() driver is opt-in; nothing runs automatically and no models
are downloaded. This module is deterministic given the reflection LM's
outputs — unit tests inject a fake reflection backend.
"""

from __future__ import annotations

import contextlib
import json
import logging
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

REFLECTION_SYSTEM_PROMPT = (
    "You are a prompt-evolution assistant (GEPA-style reflective mutation). "
    "Given the current role prompt and concrete failure traces, propose ONE "
    "improved prompt that fixes the failure modes WITHOUT weakening what "
    "already works. Reply with ONLY a JSON object: {\"prompt\": \"...\", "
    "\"rationale\": \"...\"}. Keep the output-format contract of the original "
    "prompt intact. No prose, no markdown fences."
)


@dataclass
class FailureExample:
    """One failed bench row, reduced to what the reflection LM needs."""

    task_id: str
    issues: list[str] = field(default_factory=list)
    error: str = ""


@dataclass
class Candidate:
    """One prompt candidate with its per-task fitness."""

    role: str
    prompt: str
    gen: int = 0
    scores: dict[str, bool] = field(default_factory=dict)

    @property
    def successes(self) -> int:
        return sum(1 for v in self.scores.values() if v)

    def dominates(self, other: Candidate) -> bool:
        """Strict Pareto domination over the same task set."""
        keys = set(self.scores) & set(other.scores)
        if not keys:
            return False
        wins = all(self.scores[k] >= other.scores[k] for k in keys)
        return wins and any(self.scores[k] > other.scores[k] for k in keys)


def load_failures(jsonl_path: str | Path, arm: str) -> list[FailureExample]:
    """Failed bench rows for one arm, in file order (deterministic)."""
    out: list[FailureExample] = []
    with open(jsonl_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("arm") != arm or row.get("success"):
                continue
            out.append(
                FailureExample(
                    task_id=str(row.get("task_id", "")),
                    issues=list(row.get("issues") or []),
                    error=str(row.get("error") or ""),
                )
            )
    return out


def mutation_user_prompt(base_prompt: str, failures: Iterable[FailureExample]) -> str:
    """Render the reflection input: base prompt + failure traces."""
    lines = [f"CURRENT PROMPT:\n{base_prompt}", "FAILURE TRACES:"]
    seen = 0
    for f in failures:
        detail = "; ".join(f.issues) if f.issues else (f.error or "unknown failure")
        lines.append(f"- {f.task_id}: {detail}")
        seen += 1
    if not seen:
        lines.append("(none)")
    lines.append(
        "Propose ONE replacement prompt as JSON {\"prompt\": ..., "
        "\"rationale\": ...}."
    )
    return "\n".join(lines)


def _parse_prompt_proposal(text: str) -> str | None:
    """Extract {"prompt": ...} from a reflection reply; tolerant like the runner."""
    import re

    data: dict[str, Any] | None = None
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(1))
        except ValueError:
            data = None
    if data is None:
        m2 = re.search(r"\{.*\}", text, re.DOTALL)
        if m2:
            try:
                data = json.loads(m2.group(0))
            except ValueError:
                data = None
    proposal = (data or {}).get("prompt")
    if isinstance(proposal, str) and proposal.strip():
        return proposal.strip()
    return None


async def propose_mutation(
    reflect_chat: Callable[..., Any],
    base_prompt: str,
    failures: Iterable[FailureExample],
) -> str:
    """Ask the reflection LM for a mutated prompt; falls back to the base.

    ``reflect_chat(system, user, model, temperature)`` returns the raw text
    (same signature shape as BenchEngine._chat without token accounting).
    Unparseable or empty proposals keep the base prompt — evolution is
    best-effort, never destructive.
    """
    user = mutation_user_prompt(base_prompt, failures)
    raw = await reflect_chat(REFLECTION_SYSTEM_PROMPT, user, "", 0.0)
    if isinstance(raw, tuple):  # tolerate (data, raw, t_in, t_out) chat shapes
        raw = raw[1]
    text = raw if isinstance(raw, str) else str(raw or "")
    proposal = _parse_prompt_proposal(text)
    if proposal:
        return proposal
    log.warning("unparseable mutation proposal; keeping base prompt")
    return base_prompt


CROSSOVER_SYSTEM_PROMPT = (
    "You are a prompt-evolution assistant (GEPA-style system-aware merge). "
    "You get TWO evolved prompts for the same role, each best on different "
    "tasks. Merge their COMPLEMENTARY lessons into ONE prompt that keeps "
    "what works in both. Drop nothing that a task list depends on. Reply "
    "with ONLY a JSON object: {\"prompt\": \"...\", \"rationale\": \"...\"}. "
    "Keep the output-format contract of the original prompts intact. No "
    "prose, no markdown fences."
)


def crossover_user_prompt(
    prompt_a: str,
    prompt_b: str,
    failures: Iterable[FailureExample],
) -> str:
    """Render the merge input: both prompts plus the current failure traces."""
    lines = [f"PROMPT A:\n{prompt_a}", f"PROMPT B:\n{prompt_b}", "FAILURE TRACES:"]
    seen = 0
    for f in failures:
        detail = "; ".join(f.issues) if f.issues else (f.error or "unknown failure")
        lines.append(f"- {f.task_id}: {detail}")
        seen += 1
    if not seen:
        lines.append("(none)")
    lines.append(
        'Merge A and B into ONE improved prompt as JSON {"prompt": ..., '
        '"rationale": ...}.'
    )
    return "\n".join(lines)


async def propose_crossover(
    reflect_chat: Callable[..., Any],
    prompt_a: str,
    prompt_b: str,
    failures: Iterable[FailureExample],
) -> str:
    """Merge two frontier prompts via the reflection LM; falls back to A.

    Same tolerance rules as propose_mutation: unparseable output keeps the
    first parent (prompt A) — the crossover never destroys a known-good
    candidate.
    """
    user = crossover_user_prompt(prompt_a, prompt_b, failures)
    raw = await reflect_chat(CROSSOVER_SYSTEM_PROMPT, user, "", 0.0)
    if isinstance(raw, tuple):  # tolerate (data, raw, t_in, t_out) chat shapes
        raw = raw[1]
    text = raw if isinstance(raw, str) else str(raw or "")
    proposal = _parse_prompt_proposal(text)
    if proposal:
        return proposal
    log.warning("unparseable crossover proposal; keeping prompt A")
    return prompt_a


def pick_crossover_pair(pool: list[Candidate]) -> tuple[Candidate, Candidate]:
    """Choose the two most complementary frontier candidates.

    Parent A is the strongest candidate (most successes, earlier gen and
    lexicographic prompt as deterministic tie-breaks). Parent B is the
    candidate that wins on the most tasks where A fails — GEPA's
    complementary-lesson heuristic; raw strength is only the fallback.
    A single-candidate pool degenerates to (A, A); the crossover then
    collapses to a mutation of A, which the caller can skip.
    """
    ordered = sorted(pool, key=lambda c: (-c.successes, c.gen, c.prompt))
    top = ordered[0]
    rest = ordered[1:]
    if not rest:
        return top, top

    def complement(c: Candidate) -> tuple[int, int, int]:
        wins_over_top = sum(
            1 for k, v in c.scores.items() if v and not top.scores.get(k)
        )
        return (-wins_over_top, -c.successes, c.gen)

    return top, sorted(rest, key=complement)[0]


def pareto_front(candidates: list[Candidate]) -> list[Candidate]:
    """Keep candidates that are best on at least one task; prune dominated.

    GEPA's selection rule: a candidate survives when there is a task on
    which nothing strictly beats it — greedy/beam local optima escape.
    Ties (identical score vectors) all survive.
    """
    return [
        c
        for c in candidates
        if not any(o.dominates(c) for o in candidates if o is not c)
    ]


async def evaluate_candidate(
    engine: Any,
    role_name: str,
    candidate: Candidate,
    tasks: Iterable[Any],
    arm: str,
) -> Candidate:
    """Re-run the bench arm with ONLY this role's prompt overridden.

    ``tasks`` are BenchTask objects; ``arm`` selects the bench arm.
    roles.SYSTEM_PROMPTS[<role>] is swapped for the candidate prompt and
    restored afterwards (finally) — concurrent evaluations share the
    process, so this must run serially.
    """
    from memnc.roles import SYSTEM_PROMPTS, Role

    role = Role(role_name)
    original = SYSTEM_PROMPTS[role]
    SYSTEM_PROMPTS[role] = candidate.prompt
    try:
        for task in tasks:
            with tempfile_workdir() as workroot:
                result = await engine.run_task(task, arm, 0, workroot)
            candidate.scores[task.id] = bool(result.success)
    finally:
        SYSTEM_PROMPTS[role] = original
    return candidate


async def evolve(
    engine: Any,
    reflect_chat: Callable[..., Any],
    role_name: str,
    arm: str,
    base_prompt: str,
    failures: list[FailureExample],
    generations: int = 3,
    eval_tasks: list[Any] | None = None,
) -> list[Candidate]:
    """GEPA loop: mutate from failures, evaluate, Pareto-prune, crossover.

    ``tasks`` are the BenchTask objects to evaluate candidates on (defaults
    to caller-supplied eval_tasks; callers should always pass them — the
    module never maps failure ids back to tasks). Returns the final Pareto
    frontier (best-on-at-least-one-task set).
    """
    if eval_tasks is None:
        raise ValueError(
            "eval_tasks (list of BenchTask objects) is required; "
            "the module never maps failure ids back to tasks"
        )
    tasks = eval_tasks
    seed = Candidate(role=role_name, prompt=base_prompt, gen=0)
    pool = [seed]
    for gen in range(1, generations + 1):
        # GEPA alternates the two operators: mutate from failures while the
        # pool is thin, crossover once the frontier holds complementary
        # specialists (a degenerate pool falls back to mutation).
        a, b = pick_crossover_pair(pool)
        if gen > 1 and a is not b:
            child_prompt = await propose_crossover(
                reflect_chat, a.prompt, b.prompt, failures
            )
        else:
            child_prompt = await propose_mutation(reflect_chat, pool[0].prompt, failures)
        child = Candidate(role=role_name, prompt=child_prompt, gen=gen)
        pool.append(await evaluate_candidate(engine, role_name, child, tasks, arm))
        pool = pareto_front(pool)
    return pool



@contextlib.contextmanager
def tempfile_workdir():
    """Bench run_task wants a work root; a throwaway dir keeps runs isolated."""
    d = tempfile.mkdtemp(prefix="gepa-eval-")
    try:
        yield Path(d)
    finally:
        import shutil

        shutil.rmtree(d, ignore_errors=True)
