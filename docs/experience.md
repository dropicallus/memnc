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

## 2026-11-14 external evidence: Anthropic "ultracode" (dynamic workflows)

ultracode is a Claude Code setting that turns on automatic dynamic
workflows: Claude writes a JavaScript orchestration script per
substantive task and a runtime executes it OUTSIDE the conversation
context - only the final answer returns to the chat. Primitives:
agent() spawns one subagent (optional JSON schema, validated with
retries), pipeline(items, fn) one agent per item, parallel() a batch,
phase() groups progress. Caps: 16 agents concurrent (env-tunable to
256), 1000 agents per run; scripts are saved in .claude/workflows/,
readable and diffable.

The quality pattern is adversarial: independent agents attack the
problem from separate angles, OTHER agents try to refute what they
found, and the run iterates until answers converge. Anthropic states
explicitly this is why a workflow reaches results a single pass
cannot. The plan lives in script variables, not in any model context
- no brief-loss, no plan drift across revisions, resumable after
interrupts.

Reading against our bench: ultracode is arm C (planner->coder->critic)
done differently. (1) Adversarial refutation instead of our one-shot
critic approve/reject - our arm B self-reflect (8/8 hard) is already
closer to their pattern than our arm C (6/8 at 7x tokens). (2)
Plan-as-code avoids planner context dilution - the exact
self-conditioning failure Codacus describes. (3) Coordination outside
the context window = our workflow_state KV / NATS layer; the
architecture thesis is confirmed from the outside. Candidate follow-up
arm C2/D: mesh with an adversarial critic agent + plan held in KV
instead of the planner's context.

## 2026-11-14 external evidence: ReAct (arXiv 2210.03629, Frank)

ReAct (Yao et al., ICLR 2023) interleaves Thought-Action-Observation.
HotpotQA failure analysis (200 hand-labeled trajectories): CoT fails
through hallucination (56% of its failures, false-positive rate 14% vs
ReAct 6%); ReAct fails through reasoning errors (47%, including
repetition loops) and non-informative search (23% - a bad search result
derails the trajectory and it struggles to recover). Best prompting is
a fallback combination: ReAct -> CoT-SC when no answer within 7 steps,
CoT-SC -> ReAct when the internal majority answer is unsure.

The finding that matters for our memory thesis: finetuning on 3,000
self-generated ReAct trajectories makes PaLM-8B outperform ALL 62B
prompting methods, and PaLM-62B outperform ALL 540B prompting methods.
Finetuning Standard or CoT teaches models to memorize (potentially
hallucinated) knowledge FACTS; finetuning ReAct teaches the
generalizable SKILL of how to reason and act to retrieve information.

This is direct empirical support for our split: procedural skill ->
weights (trainable, generalizable), facts -> external (retrieved, never
memorized). Connections: (1) arm D doubly motivated - Codacus says the
small specialist needs search, ReAct gives the concrete format (and
shows act-only without reasoning traces is worse than the interleave);
(2) the finetuning result is the capacity argument for growing-LoRA: a
small model with the trained procedure beats a big model with
prompting; (3) the internal-unsure-then-external fallback is a concrete
routing signal for the pheromone slice 14.15.10.

## 2026-11-14 external evidence: FireAct - the direct ReAct follow-up

FireAct (arXiv 2310.05915, Oct 2023, co-authored by Shunyu Yao, the
ReAct first author) takes ReAct section 3.3's finetuning aside and
makes it systematic: Llama2-7B finetuned on 500 GPT-4-generated ReAct
trajectories gains +77% on HotpotQA (EM 14.8 -> 26.2); finetuned
Llama2-13B beats ALL GPT-3.5 prompting methods. Multi-method
finetuning (ReAct + CoT trajectories mixed, EM 41.0) beats ReAct-only
(39.4): the model learns to answer easy questions internally in one
round and search only when hard - the ReAct fallback heuristic placed
IN THE WEIGHTS instead of the prompt. Robustness: with a search API
returning 50% junk, prompted ReAct drops 28-34% but finetuned FireAct
only 5-14% (training trajectories contained successful noise
handling). Inference is 70% cheaper (no few-shot prompt in context).
Llamas need ~500 trajectories before the ReAct format emerges.

memnc relevance: FireAct is distill-big-trajectories-into-small-open-
models in production - the growing-LoRA shape for the agent mesh. Our
successful bench trajectories are exactly the training source this
recipe consumes; a mesh node learning from its own bench history is a
candidate future arm E. Wider lineage for reference: Reflexion
(2303.11366, verbal RL - our arm B pattern), ReWOO (2305.18323, plan
once with placeholders, 5x cheaper - plan-in-KV idea), LATS (2310.04406,
ReAct + tree search).

## 2026-11-14 external evidence: GEPA (ICLR 2026, Frank)

GEPA (Genetic-Pareto; Agrawal et al., the DSPy team - Berkeley,
Stanford, Databricks, MIT) argues that natural-language reflection is a
richer learning medium than policy gradients from sparse scalar
rewards. Weights stay frozen; GEPA evolves only the prompts of a
compound system. Three components: (1) reflective prompt mutation -
rollout traces plus evaluation traces (e.g. compiler errors) are fed
to a reflection LM that does implicit per-module credit assignment and
proposes targeted prompt updates; (2) Pareto-based candidate selection
- a candidate stays in the pool when it is best on at least one task,
strictly dominated ones are pruned, which escapes local optima that
greedy/beam selection fall into; (3) system-aware merge, a crossover
that combines complementary lessons from the Pareto frontier.

Results: beats GRPO by +6pp on average (up to +19pp) with up to 78x
fewer rollouts (IFBench: 678 rollouts -> 38.6% vs GRPO's 24,000
rollouts -> 35.9%); beats MIPROv2 by +10pp. Reflectively evolved
instructions beat optimized few-shot examples and are up to 9.2x
shorter; prompts evolved on Qwen3-8B transfer to GPT-4.1-Mini (+9pp).

memnc reading: GEPA is arm B's self-reflection used as an OFFLINE
optimizer over a bench history instead of online per task. All
ingredients already exist in this repo: bench JSONL traces, our
deterministic checkers as the feedback function (text feedback is the
checker output), and modular SYSTEM_PROMPTS in roles.py. Candidate
slice: bench-driven prompt evolution of the planner/coder/critic
prompts with zero weight changes. It also sharpens the skill->weights
ladder: prompt -> prompt evolution (GEPA) -> finetuning (FireAct,
LoRA). GEPA is the cheap middle rung - most of the gain for orders of
magnitude less compute, before any growing-LoRA work. The Pareto
frontier itself mirrors the Einzel-LoRA thesis: GRPO collapses onto
one averaged policy the way multi-task finetuning does, while GEPA
keeps diverse specialists each winning on different tasks - which maps
directly onto pheromone routing (14.15.10): per-node evolved prompt
variants as specialization, routing picks the Pareto winner per task
class.

## 2026-11-14 external evidence: ACE (ICLR 2026, Frank)

ACE (Agentic Context Engineering; Zhang et al., Stanford/SambaNova/
Berkeley, ICLR 2026) treats contexts as EVOLVING PLAYBOOKS: a
structured, itemized collection of bullets (each with helpful/harmful
counters, grouped in sections) that accumulates, refines and organizes
strategies. Three specialized roles: the Generator produces reasoning
trajectories, the Reflector distills concrete lessons from successes
and errors (JSON: error identification, root cause, correct approach,
key insight), and the Curator emits compact DELTA operations
(ADD/UPDATE/REMOVE) that are merged by lightweight deterministic
non-LLM logic - grow-and-refine, never a monolithic rewrite.

Two named failure modes of prior context adaptation: (1) brevity bias
- LLM summarization drops domain insights for conciseness; (2) context
collapse - iterative monolithic rewriting erodes accumulated detail.
ACE's itemized deltas preserve detail, allow parallel merges, and
enable lazy/periodic pruning.

Results: +10.6% on agents and +8.6% on finance. AppWorld: base 42.4%,
ICL 46.0%, GEPA 46.4%, Dynamic Cheatsheet 51.9%, ACE 59.5% - matching
a production leaderboard agent with a smaller open model. Beats GEPA
with 75.1% fewer rollouts, 86.9% lower adaptation latency (delta
updates instead of GEPA's prompt-validation rollouts). Works offline
(system prompts) AND online (agent memory), and adapts WITHOUT labels
- natural execution feedback suffices. Caveat: without any reliable
feedback signal, ACE and Dynamic Cheatsheet DEGRADE (spurious signals
pollute the playbook) - feedback quality is the critical ingredient.

memnc reading: this is the automated version of our Erfahrungsspeicher
(docs/experience.md) - and we have exactly the reliable feedback it
needs (deterministic checkers). The three roles map 1:1 onto our mesh
topology: coder=Generator, critic=Reflector, planner=Curator writing
into workflow_state/backlog KV. Concrete design rule for any memnc
memory store: append-only itemized bullets with counters, merged
deterministically, pruned periodically - NEVER let an LLM rewrite the
whole store (context collapse). Combined with GEPA the ladder is now:
prompt -> prompt evolution (GEPA) -> evolving context playbooks (ACE)
-> finetuning (FireAct/LoRA). ACE and GEPA are complementary: GEPA
mutates the prompt, ACE accumulates context; ACE won on AppWorld.

## 2026-11-14 external evidence: MEM1 (MIT, ICLR 2026, Frank)

MEM1 (Zhou et al., MIT - Rus/Liang groups, arXiv 2506.15841; ICLR
2026, Best Paper at the NeurIPS 2025 MTI-LLM workshop) trains agents
via RL (PPO over veRL) to operate with a CONSTANT compact internal
state: at every turn the model itself consolidates prior memory plus
new environment observations and strategically discards irrelevant or
redundant information. Memory management is not prompted - it is a
LEARNED skill in the weights.

Results: MEM1-7B improves performance 3.5x while using 3.7x less
memory than Qwen2.5-14B-Instruct on a 16-objective multi-hop QA task,
and generalizes beyond the training horizons. Checkpoint released
(Qwen2.5-7B, MIT license); builds on veRL, search-R1 and
AgenticMemory.

memnc reading: this is the direct answer to the "chatstrophic
forgetting" question - what to keep and what to discard is TRAINABLE.
MEM1 and ACE are the exact poles of the memory spectrum: ACE
consolidates externally (deterministic playbook merge, zero training,
needs reliable feedback), MEM1 consolidates internally (RL trains the
consolidation behavior itself). Natural memnc path: ACE-style external
playbook first (cheap, immediate), MEM1-style RL consolidation as the
later growing-LoRA stage. Operational bonus: constant context bounds
each node's KV size - relevant to capacity planning and routing
(14.15.10); our arm C ReadTimeouts partly came from growing contexts.

## 2026-11-14 external evidence: SyncMind (arXiv 2502.06994, Frank)

SyncMind (Guo et al., UIUC / Heng Ji group, Feb 2025 - PREPRINT, not
peer reviewed) defines the OUT-OF-SYNC problem in collaborative SE: an
agent works on a task while the shared environment evolves underneath
it (another participant commits at T_j); at T_k its understanding
diverges from the true state and its actions fail, causing integration
breakage. SyncBench operationalizes it: 24,332 out-of-sync instances
derived from 21 real GitHub repositories, with executable verification
tests (same checker philosophy as our bench).

Findings: enormous model gap in recovery (Llama-3.1 agents <= 3.33%,
Claude-3.5-Sonnet >= 28.18% - even the best is far from solved);
collaboration willingness <= 4.86% - agents almost never ASK for help
or sync when out-of-sync, a fundamental limitation; when collaboration
does happen it positively correlates with recovery success; and
near-zero resource awareness (minimal performance difference between
resource-aware and unaware recovery - agents ignore their own
budgets).

memnc reading: (1) out-of-sync IS the distributed-memory problem -
our node revocation tombstones (21.3), workflow_state KV and
pheromone freshness are sync mechanisms for exactly this failure mode;
(2) the <=4.9% willingness number proves collaboration must be
architecturally FORCED, not left to the agent - our forced
planner->coder->critic pipeline is vindicated, and future coder nodes
should get an explicit sync-check prompt on suspicion of drift;
(3) candidate future bench tier: a task that runs while a simulated
second node commits, where the coder must reconcile - would
discriminate hard (frontier models score ~28%); (4) routing must
enforce resource discipline from the outside (pheromone/capacity
signals) because agents will not self-regulate.

## 2026-11-14 external evidence: Flow-DPO (NeurIPS 2024, Frank)

Flow-DPO (Deng & Mineiro, UCLA + Microsoft Research, arXiv 2410.22304,
NeurIPS 2024 main conference) builds a FLOW of two LLMs that share one
base model with SEPARATE LoRA adapters: an Answer LLM emits bounded
chunks of the solution (chunk size 160, max 6 steps), a Stop LLM
Yes/No-decides whether the partial answer is complete; loop until Yes.
This is essentially a two-role collaboration loop - a mini version of
our coder/critic pipeline.

The contribution is the training: ONLINE DPO with rollouts. At each
output node of the initial generation a random rollout continues to a
second final answer; when original and rollout differ in correctness,
that becomes a DPO pair (chunk leading to the correct result =
preferred). Both roles are trained; the Stop LLM is evaluated on its
stopping decisions - a denser reward signal than outcome-only.

Results: progressive validation accuracy of the Llama-3-8B flow
improves 46.5% -> 66.0% over 2000 training samples, while the
UNTRAINED flow sits UNDER the single model (46.5 vs 51.0 zero-shot) -
the collaboration structure alone does not help; training makes the
difference. Compile test (same prompts, fair trace-quality comparison
via SFT): SFT on Flow traces beats self-generated traces AND even
ground-truth traces (GSM8K 71.3 vs 68.8 vs 67.2; MATH 27.8 vs 24.2 vs
25.1). Cost: 36-48h on 4x A100 for the DPO - real training compute,
unlike GEPA/ACE.

memnc reading: this is the missing ladder rung. ReAct (skill is
finetunable) -> FireAct (static trajectory SFT) -> GEPA (prompt
evolution, zero training) -> ACE (external playbook store) -> MEM1
(internal RL consolidation) -> Flow-DPO (THE MESH PRODUCES ITS OWN
TRAINING DATA ONLINE: the collaboration loop itself generates DPO
pairs, and each role learns from the same trace). Two roles on one
base with separate LoRAs is exactly Frank's growing-LoRA architecture
- specialists as adapters on a shared base, not multi-model monsters.
The critic trained on stop decisions points at our verify-gate:
checker results are stopping supervision, so the gate can become a
learned verifier. And our bench (seeded reps + deterministic
checkers) IS the DPO pair generator - preferred vs rejected chunks
fall out of the runs we already log. Same constraint as ACE: needs
reliable feedback; math/code with checkers qualifies, free chat does
not.

## 2026-11-15 external evidence: multi-agent poetry in non-cooperative environments (Zhang & Eger, Frank)

"LLM-based multi-agent poetry generation in non-cooperative
environments" (Zhang & Eger, Univ. Mannheim / UTN Nuremberg, arXiv
2409.03659, PEER-REVIEWED: Journal of Language Modelling, published
2026-02-20; code/data released). A 4-agent social network with BOTH
cooperative interactions ("poets appreciate each other's work and
learn from the others") and NON-cooperative interactions ("poets
dislike each other's work and deviate from each other"). Evaluation
over 96k generated poems, with training-based agents (GPT-2) and
prompting-based agents (GPT-3.5/GPT-4).

Findings: (1) non-cooperative environments boost diversity -
training-based agents gain +3.0-3.7pp diversity and +5.6-11.3pp
novelty (distinct/novel n-grams), with the JOINT positive+negative
learning strategy most effective; (2) training-based agents show
STABLE group divergence in lexicon, style and semantics along their
predefined group affiliation; (3) the decisive contrast: prompting-
based agents barely improve lexical diversity after the first
iteration, show NO group-based divergence, and their styles
HOMOGENIZE over time (authors suspect LLM memorization) - a
non-homogeneous (heterogeneous-model) ensemble adds +7.0-17.5pp.

memnc reading: this is the strongest external evidence yet for the
Einzel-LoRA thesis. Identical structure, two learning modes, clear
result: SPECIALIZATION IN WEIGHTS STAYS DURABLE, PROMPTED
SPECIALIZATION DECAYS TO HOMOGENEITY - ACE's context collapse,
measured at agent level. Design consequences: (a) a
deviate/disagree step belongs in our cooperative pipeline - the
adversarial critic idea for arm C2/D gains a diversity argument on
top of the correctness argument; (b) heterogeneous nodes per mesh
(different models per node) are a feature, not a workaround. Caveat:
creative domain, metric is diversity/novelty not correctness -
transfers as hypothesis, not bench-grade proof.

(Series complete: ReAct -> FireAct -> GEPA -> ACE -> MEM1 -> SyncMind
-> Flow-DPO -> this. Research-material intake paused per Frank.)

## 2026-11-15 external evidence: TalkHier - Talk Structurally, Act Hierarchically (IEEE TAI, Frank)

"Talk Structurally, Act Hierarchically: A Collaborative Framework
for LLM Multi-Agent Systems" (Zhao Wang, Sota Moriyama, Wei-Yao
Wang, Briti Gangopadhyay, Shingo Takamatsu; Sony Group Corporation,
arXiv 2502.11098, PEER-REVIEWED: IEEE Transactions on Artificial
Intelligence (TAI); code: github.com/sony/talkhier).

Two core mechanisms: (1) a STRUCTURED COMMUNICATION PROTOCOL - every
communication event is an explicit tuple (Message, Background,
Intermediate Output) instead of free text; supervisor prompt template
demands: thoughts -> who acts next (or FINISH) -> instructions ->
background -> intermediate_output. (2) HIERARCHICAL REFINEMENT - the
evaluation step is its own nested team: an Evaluation Supervisor
coordinates k member evaluators, each evaluating with exactly ONE
criterion; feedback is summarized and coordinated (fixing order bias
and summarization failure as agent count grows) and handed to a
Revisor. Members of one team can be supervisors of another team.
Each agent keeps an INDEPENDENT persistent memory - deliberately not
a shared pool and not session-bound.

Results (GPT-4o backbone, temp=0): MMLU 5-domain avg 88.38% vs
o1-preview 87.56, AgentVerse 83.66, AgentPrune 83.60, GPT4o-7@
majority voting 71.15, ReAct 58.50; WikiQA Rouge-1 0.3461 vs 0.3286
best baseline (AutoGPT); advertisement-text generation wins on all
quality metrics. Ablations (the most informative part): removing the
evaluation team -11.1pp; removing the evaluation supervisor -5.4pp;
structured protocol -> flat text -2.8pp; removing the Background
field -8.9pp (the biggest single protocol factor); removing
Intermediate Output -4.7pp.

memnc reading: (1) the Background ablation (-8.9pp) validates the
pattern our bench uses implicitly - every role receives the full
task context; TalkHier generalizes it: NATS messages between roles
should be structured tuples (message, background, intermediate
output) with background persisted in workflow_state, not prose.
(2) Critic split: our critic is one agent with all criteria at once;
TalkHier's k single-criterion evaluators + coordinated supervisor is
a concrete design for arm C2/D - with the caveat that k evaluators
per iteration is the same cost-multiplier class that put our arm C
at 7x arm A. (3) Role-specific persistent memory (per-node
namespaces) vs one shared pool is a concrete alternative for the
mesh memory design that the Einzel-LoRA thesis also supports.

(FINAL research-material intake item per Frank - series complete:
ReAct -> FireAct -> GEPA -> ACE -> MEM1 -> SyncMind -> Flow-DPO ->
non-cooperative poetry -> TalkHier.)

## 2026-11-15 external evidence: Multi-agent AI fundamentals (Electronic Markets, Frank)

"Multi-agent AI" (Simeon Allmendinger, Lukas Bonenberger, Kathrin Endres, Dominik
Fetzer, Henner Gimpel, Niklas Kuehl; University of Bayreuth / Fraunhofer FIT /
University of Hohenheim / TRUSTEQ; Electronic Markets (2026) 36:18,
https://doi.org/10.1007/s12525-025-00862-z; PEER-REVIEWED journal, received
2025-06-05, accepted 2025-11-28). FUNDAMENTALS/CONCEPTUAL paper: no experiments,
no benchmark - it is a framework + research-agenda piece. Evidence class:
peer-reviewed but conceptual, so it carries no performance numbers; its value is
definitional and as a named counter-position.

Content: MAAI (multi-agent artificial intelligence) conceptualized as a layered
five-component architecture: C1 foundation model, C2 data-centric perception and
action, C3 dynamic orchestration, C4 agent-integrated workflow, C5 interaction
interface. Three interdependent research pathways: technical capabilities
(agent modularity, orchestration logics, VALIDATION METHODS for reliability when
workflows are dynamically composed from opaque FMs), organizational integration
(hybrid human-AI work arrangements, process mining to make emergent agentic
workflows transparent and optimizable), socio-technical (fairness, accountability,
labor transformation). 11 numbered RQs across the three pathways.

Two details that matter for memnc:
1. C2 is explicit data-centric-AI doctrine: "the architecture and capacities of
   the foundation model remain fixed, its effective adaptation is based on the
   data it receives" - RAG, prompt templates, retrieval, never weights. This is
   the mainstream paradigm Frank rejects (parametric memory through weights).
   With this paper the counter-position to the Einzel-LoRA thesis now has a
   peer-reviewed named formulation - the memnc benchmark (self-reflect/mesh vs
   weight-specialized variants) is the falsification structure against it.
2. The framework maps 1:1 onto the mesh: C3 (registry of role-specific agents,
   moderator agent, ordered vs disordered cooperation, cooperative vs adversarial
   interaction) = roster + planner + debate loop; C4 (workflows dynamically
   generated in response to emergent information needs, van der Aalst design
   patterns from sequential/parallel pre-defined to iterative loops
   innovation/lifecycle-oriented) = backlog + state machine; C5 = meshctl/console.
   Their demanded "validation methods for dynamically composed opaque agents" is
   exactly memnc's deterministic checker + verify-gate; their process-mining
   agenda for emergent workflows is exactly trace/roster observability.
