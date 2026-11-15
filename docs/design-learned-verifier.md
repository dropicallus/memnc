# Learned verifier (Flow-DPO slice, step 2 — design note, before coding)

Follow-up to the DPO pair generator (`src/memnc/bench/dpo.py`, BUILT).
This note designs the *learned verifier* deliverable: a verifier that
scores a candidate solution without running the checker, trained on the
bench's own (chosen, rejected) pairs.

## Why a learned verifier at all

The deterministic checkers are ground truth, but they only exist for bench
tasks. Real mesh tasks have no checker: the verify-gate today relies on the
critic's judgment alone (and the fabrication incident of 2026-11-15 showed a
critic approving an empty result). A verifier trained on
checker-supervised pairs gives the gate a *prior* over solution quality
that was learned from real evidence, not invented by the critic.

## Two candidate designs (scale ladder)

### V1 — cheap linear scorer (no training compute, buildable now)

Features extracted from a solution's files dict (deterministic, no model):
file count, total bytes, line count, AST-parse success flag, comment
fraction, duplicate-line ratio, presence of obvious placeholders
(`TODO`, `pass`, empty files, fabricated-metric-shaped constants).
Trained as a logistic regression over pairs (chosen vs rejected) with a
pure-python optimizer — no new dependency. Weights stored as a JSON
artifact; `verify(files) -> score`.

Value and hard limit: it can only catch *shape* failures (empty files,
placeholder stubs — the exact fabrication failure mode), never semantic
wrongness. Honest scope: a regression test for fabrication, not a
competence judge. It complements, never replaces, the checkers.

### V2 — LoRA verifier on the shared base (needs compute approval)

Real Flow-DPO step: fine-tune a small base model as a verifier on the
accumulated pairs (multi-epoch, the pairs repeat across seeded reps so the
effective dataset is larger than the task count). This is the peer-grade
deliverable and the rung toward the growing-LoRA architecture (a
verifier adapter alongside the role adapters). Blocked on: user approval
for training compute + a model download; a `record_solutions` bench run
with enough pairs (the current runs do not record solutions).

## Data readiness (verified)

- `/tmp/bench-full.jsonl` (the 120-rep run): 0 rows carry `solutions` —
  it was produced without `record_solutions=True`.
- Any live verifier training needs a dedicated run with
  `record_solutions=True` on the hard tier (8 tasks x reps) to build the
  pair set first.

## Build order when V1 is green-lit

1. `src/memnc/bench/verifier.py`: feature extractor + logistic training
   over `dpo.build_pairs` output + `verify()` scorer (pure stdlib).
2. Deterministic unit tests on synthetic pairs (a passing solution vs an
   empty/placeholder file) — no network, no model.
3. Evaluation-only: on a recorded run, report the verifier's accuracy
   against checker outcomes (report-only; the checker stays the gate).
4. V2 stays a separate, human-approved slice (LoRA training).

## Non-goals

- No verifier output ever gates the bench loop (leakage rule).
- No new model downloads without explicit user approval.
- No sklearn/torch dependency for V1.
