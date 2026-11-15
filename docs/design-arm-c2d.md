# Arm C2/D design — adversarial refinement mesh (DESIGN, not built)

Status: DESIGN 2026-11-15 (night). Written BEFORE any code, per the
write-before-code rule. Priority 3 of roadmap section 15.3.

## Problem statement

Hard-tier bench: A (single coder) 6/8 at ~610 tokens/task; B (self-reflect
coder) 8/8 at ~2144; C (planner+coder+critic mesh) 6/8 at ~4369 — 7.2x A's
cost for zero success gain over B. Any new mesh mechanism must beat B, at a
cost materially below C. Arm D is the proposal for that.

## Evidence going into the design

- Ultracode (praxis): the winning review pattern is adversarial refutation —
  the reviewer must try to BREAK the artifact, not opine on it. Arm B's
  self-reflect loop is a weak internal version of this.
- Non-cooperative poetry (J. Lang. Modelling 2026): prompted agents
  homogenize and stop improving after iteration 1; an explicit
  deviate/disagree step adds diversity and novelty. A critic that "agrees
  politely" is the failure mode to design against.
- TalkHier (IEEE TAI): structured tuples (Message, Background, Intermediate
  Output) with a persistent Background field were the biggest single protocol
  factor (-8.9pp); decomposing evaluation into k single-criterion evaluators
  with a coordinating supervisor removed order bias (-11.1pp when removed) —
  but k-evaluator cost is exactly the multiplier class that sank arm C.
- Codacus (praxis): planner = the biggest model available; specialists may
  need search/harness tools rather than more autonomy.
- ReAct (ICLR 2023): an agent that concludes "internal unsure" should
  escalate outward instead of guessing — usable as an early-abort signal.

## Variants

### D1 — adversarial single round (cheapest, test first)

Flow: planner (once) -> coder -> critic -> (reject -> coder revise) -> verify.

The only change vs C is the CRITIC CONTRACT: the critic may not produce a
verdict without first attempting a concrete counterexample — a specific input,
edge, or line that breaks the code (or an explicit "tried X, Y, Z; no
counterexample found" list). This is the poetry deviate step operationalized:
the critic MUST disagree-or-prove-tries, agreeability without evidence is a
reject of the critique itself.

Max 2 rounds (not 3) to bound cost. Verify-gate remains final authority
unchanged.

Cost prediction: ~A + 2 critique calls ≈ 1600-2200 tokens/task — the B class,
not the C class.

### D2 — TalkHier evaluator split (only if D1 < B)

Replace the single critic with k=2 single-criterion evaluators + one eval
supervisor:
- E1 criterion: algorithm logic correctness (does the approach solve it).
- E2 criterion: deterministic-contract compliance (checkers.py inputs/outputs,
  edge cases the checker will exercise).
The supervisor merges the two into ONE concrete revision instruction
(no summarization-by-vote, explicit merge). k is capped at 2 — the arm C cost
curve is the standing warning; if D2 exceeds 3000 tokens/task mean, it is
disqualified regardless of accuracy.

### D3 — structured tuples + plan-in-KV (orthogonal, applies to all arms)

Planner output is persisted once as a background tuple in workflow_state:
{plan, assumptions, task_contract}. Coder and critic receive (message,
background, intermediate_output) tuples instead of re-derived prose. This is
the TalkHier Background field: persistent, shared, not re-summarized per hop.
Cheap (zero extra calls), so it ships with D1 rather than behind it.

## Success criteria (hard tier, 8 tasks x 5 seeded reps)

- D1 qualifies if: >= 7/8 success AND mean tokens < 3000 (clearly below C).
- D1 becomes the default mesh mode if it matches B (>= 8/8) at <= 1.2x B cost.
- If D1 < 7/8: run D2 (evaluator split). If D2 also < B: mesh-as-refiner is
  DEAD for this task class; keep mesh only as supervisor-of-one (weak->strong
  routing, 14.15.3) and route the difference into the GEPA prompt-evolution
  slice (15.3 item 4) instead.
- Per-task diagnostics: which of A's two hard failures (rate_limiter,
  dijkstra) each variant rescues; a variant must rescue at least one to count
  as progress.

## Non-goals

- No search/harness tools in arm D v1 (Codacus angle) — tool surface is a
  separate slice; D must first prove that STRUCTURE alone beats B.
- No model downloads, no new nodes. Same model set as the committed bench.
- No k>2 evaluators, no nested teams (TalkHier's full hierarchy) — cost class
  ruled out by arm C.
