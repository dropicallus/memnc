# Full seeded run — hard tier, 3 arms x 8 tasks x 5 reps (2026-11-15)

Single seeded run, 120 rows, all completed. Runner flags: `--arms A,B,C
--reps 5 --tiers hard`, INFERENCE_TIMEOUT_S=1200, base ollama localhost:11434.
Source data: /tmp/bench-full.jsonl (session artifact; numbers below are the
durable record).

## Pooled results with Wilson 95% CIs

| Arm | Success | Rate | Wilson 95% CI | tokens/task | wall/task |
|-----|---------|------|----------------|-------------|-----------|
| A single coder | 33/40 | 82.5% | [68.0, 91.3] | 609 | 89 s |
| B self-reflect | 38/40 | 95.0% | [83.5, 98.6] | 1910 | 147 s |
| C mesh | 35/40 | 87.5% | [73.9, 94.5] | 4295 | 1040 s |

The B and C intervals overlap heavily; the ordering B > C is not
statistically resolved, but B is never worse and costs 2.3x fewer tokens and
7x less wall time. Tokens per successful run: A 738, B 2011, C 4909.

## Per-task rescue matrix (successes out of 5 reps)

| task | A | B | C |
|------|---|---|---|
| hard_bst | 5 | 5 | 5 |
| hard_dijkstra | 3 | 5 | 4 |
| hard_longest_substr | 5 | 5 | 5 |
| hard_matrix_rotate | 4 | 5 | 5 |
| hard_rate_limiter | 0 | 4 | 1 |
| hard_tokenizer | 5 | 5 | 5 |
| hard_topo_sort | 5 | 5 | 5 |
| hard_word_ladder | 5 | 5 | 5 |

Two observations:

1. hard_rate_limiter is the discriminator. A 0/5, B 4/5, C 1/5 — the task
   punishes exactly the failure mode single-shot and full-mesh share (a
   non-monotonic timestamp trap) and rewards the self-reflect loop's
   revise-without-replanning behaviour. C's planner restarts actively hurt
   here.
2. hard_dijkstra is B's unique rescue (A 3, C 4, B 5): the model's own
   shortest_path TypeError gets caught by reflection. C does not add on top
   of B anywhere — no task where C > B.

## Consequences

- Confirms 15.1: self-reflection is the cost-effective winner; mesh buys
  zero additional success at 2.3x token / 7x wall cost on this tier.
- Routing prior (14.15.10) initialized from this table, see roadmap.
- Arm D (docs/design-arm-c2d.md) must beat B = 95%, CI [83.5, 98.6], at
  <= 1.2x B cost; D1's "adversarial single round" is the candidate.
