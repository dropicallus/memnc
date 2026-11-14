# Experience store — what we know about our models

Purpose: a shared, versioned experience memory, committed to the repo.
Generic benchmarks measure abstract capability; this file records what OUR
models actually do on OUR tasks. Frank's framing (2026-11-14): "Eine Art
Erfahrungsspeicher den man teilt — was können die Modelle gut, was nicht,
welche Tasks gibt man dem einen, welche dem anderen, welche besonderen
Charakter-Eigenschaften ergeben sich im Modell."

Two layers:
- **This file** — distilled, human-readable, git-versioned. Evidence-backed.
- **KV pheromones** (roadmap 14.15.10) — the numeric hot layer the mesh
  reads at claim time.

## Update rule

Every bench run and every notable live task appends or amends an entry.
Each claim carries evidence (task / run / date / metric). An entry without
evidence is marked `hypothesis`. Entries contradicted by data get corrected,
never deleted — the history is the point.

## Entry schema

```
## <model> (<role>, <node>)
Good at: ...
Bad at: ...
Assigned task types: ...
Character traits: ...
Evidence: <task/run/date/metric>
Status: hypothesis | confirmed
```

## Entries (initial — from live runs, 2026-11-14)

## qwen2.5-coder:32b (coder, node-a)
Good at: end-to-end coding tasks through the debate loop (night3 live
proof: planner -> coder -> critic APPROVED -> done).
Bad at: t.b.d. — bench-report will quantify per tier.
Assigned task types: all coding tasks (current default).
Character traits: t.b.d.
Evidence: night3 run (2026-11-14); bench-report pending.
Status: hypothesis.

## qwen2.5-coder:7b (planner, node-c)
Bad at: strict JSON output — produced "no JSON object found" once
(node-c as orchestrator, 2026-11-14; did not recur).
Assigned task types: planning only, never coding.
Character traits: t.b.d.
Evidence: 1 observed failure (n=1).
Status: hypothesis.

## critic model (7b, debate loop)
Bad at: rejects correct code — hallucinated blockers on code that passed
the deterministic checks. Over 40 live tasks only 2 revisions occurred:
it approves almost everything on proof-of-concept tasks.
Character traits: sycophantic under pressure (matches Du et al. 2023,
"Talk Isn't Always Cheap" 2025).
Consequence: verify-gate policy — deterministic checks passing override
critic rejection (worker.py ~1114).
Evidence: 40 live tasks, driver logs.
Status: confirmed.

## Open questions (Frank)
- Generic benchmarks are "zu unklar" for our purposes: the bench (14.15.9)
  measures success rate per tier, but character traits (verbosity, stubbornness,
  sycophancy, planning style) need their own observation channel.
- Which tasks go to which model should eventually be decided by pheromones
  (14.15.10), with this file as the human-auditable explanation.

## 2026-11-14 bench smoke (easy tier, rep 0, ollama qwen2.5-coder:32b)

First live bench-report (docs/bench/report-easy-smoke-2026-11-14.md):
24/24 runs passed. All three arms at 100% on easy - the ceiling effect
predicted by "More Agents Is All You Need" (gains correlate with task
difficulty). The easy tier cannot separate the arms; what it DOES show
is the cost side: mean wallclock A 50s / B 53s / C 420s and mean tokens
A 435 / B 854 / C 3207. The mesh is 7.4x the token cost of a single
shot on tasks a single model already solves. Discriminating evidence
must come from medium/hard tiers where single-shot actually fails.

## 2026-11-14 bench medium tier (rep 0, ollama qwen2.5-coder:32b)

Medium: 24/24 passed, all arms 100% - qwen2.5-coder:32b solves the whole
medium tier single-shot. Cost gap persists: tokens A 521 / B 1913 /
C 4346 (8.3x), wallclock A ~52s / C ~10min. Interesting: arm B used
0.75 self-critique rounds on average (the model sometimes reflects even
when right), arm C needed 1.88 revision rounds. Difficulty must come
from the hard tier; easy AND medium are saturated by the strong coder
model. Next: hard tier overnight; consider weaker coder for tier
contrast or more reps for tighter CIs.

## 2026-11-14 bench hard tier (rep 0, ollama qwen2.5-coder:32b) - THE discriminator

Hard: A 6/8 (75%), B 8/8 (100%), C 6/8 (75%). The single tier that
separates the arms - and the winner is SELF-REFLECTION, not the mesh:
B rescued both A-failures (rate_limiter burst rule, dijkstra edge case)
with just 0.75 critique rounds on average. C matched A, not B, at 7x
the token cost (mean 4369 vs 610 in+out per task) - one legit failure
(rate_limiter, same wrong burst rule as A) and one infra artifact
(ReadTimeout at 2070s: the 32b backend exceeded the default inference
timeout on a long revision turn; runner labels model-code exceptions
as 'checker error' too, which reads misleading). Full-run notes:
(1) raise the inference timeout for hard-tier mesh runs, (2) the B-vs-C
gap is now first REPO-OWN evidence for the single-agent>multi-structure
finding - more structure bought zero success over self-reflection.
Next: full 3-tier x 3-arm run with >=5 seeded reps for tight CIs, and
the harness arm question (execution tools) stays open as arm D.

## 2026-11-14 external evidence: Codacus video + weightpatch (planner sizing + search for specialists)

Two external sources, read at Frank's request, both map onto the bench
results and the routing roadmap.

Codacus video (youtube PZQWdlTi9B0, "Is this how Local AI reaches
Frontier Capabilities?", 2026-11-14): a team of small specialists on one
RTX 3060 LOST to its own planner running alone on 4 real jobs with
pre-locked checkers. His root cause was the BRIEF - experts only saw
the planner's summary, never the original request. We verified our
runner does NOT have this failure mode: task.prompt goes to coder AND
critic in every arm (runner.py:171/260/342), so our B-vs-C gap is
CLEANER evidence than his - structure itself failed, not information
loss at the handoff. Two further findings: (1) per-step-easy,
whole-job-fails - a 30-step terminal job compounds small mistakes via
self-conditioning (own error in context raises next-error probability;
terminal-bench spread GPT-5.5 85% vs T-Max-9B 27%). This explains why
our arm B self-reflection won: reflection breaks the loop. (2) His
weight-editing attempt (Sparks of Weight-Space Programming / Boncio
toolkit) confirms weight surgery only works on one clear bug at one
place (24% -> 16-17%, model reroutes); a habit spread across a long job
is a decision repeated at every step - needs a better planner, not
neuron removal. Support for the parametric-memory thesis: real capacity
comes from training (Carbon-X mid-training on 300B DNA tokens), not
surgery.

Core recommendation (Frank-emphasized): the PLANNER must be big - the
best model the card fits - and the small specialists need SEARCH,
because facts do not fit in small weights ("the student goes to the
library"). Skill lives in weights (coaching), facts come from outside
(library). For memnc: (a) the planner role should route to the
strongest node - a planner-capacity bonus for the pheromone-routing
slice 14.15.10; (b) "specialist + search tools" is the clean arm-D
hypothesis (our bench agents run tools=None by design, so arm D =
execution/retrieval tools, not just a harness); (c) no RAG conflict -
the split matches ours: procedural skill -> weights, facts -> retrieve.

weightpatch (github SuperMarioYL/weightpatch v0.1.0, MIT): the missing
HANDOFF layer for weight edits. diff two safetensors dirs into a sparse
.wpatch (changed slices only, raw machine words, BF16 bit-exact),
apply/revert (byte-identical)/stack, and screen verifies which edited
slices survived requantization into a single-file GGUF (name remap,
GGML ne-order transposition, packed blocks). Content-addressed id,
per-tensor before_sha256 pre-image check. Directly relevant to the
growing-LoRA / Fisher-merge memory vision: sparse, revertable,
content-addressed patches with a post-quantization check are a
candidate transfer format for weight-borne memory BETWEEN nodes.
