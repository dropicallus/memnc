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
