# Implementation Roadmap: Experience Graph (Steps 1–3)

This document outlines the concrete implementation tasks to build the **Experience Graph** in the `memnc` mesh, transitioning from simple task orchestration to a learning-capable, stigmergic fabric.

## Overview
The Experience Graph is designed to capture, store, and eventually leverage the "experience" of the mesh. This is built through a dependency-ordered sequence to ensure measurement precedes behavior.

---

## Phase 1: The Learning Pipeline (Steps 1-3)

### Step 1: Deposit Wiring (Observation Capture)
**Goal:** Automatically persist task transitions into a queryable observation log.
**Status:** Code-ready (Step 0: Schema is complete in `src/memnc/observations.py`).

| Task | Description | Targeted Files | Interface/Key Changes | Acceptance Criteria |
| :--- | :--- | :--- | :--- | :--- |
| **1.1 Orchestrator Hooks** | Integrate `observation_from_snapshot` into the orchestrator's state machine transitions. | `src/memnc/orchestrator.py` | `_apply(event)` / `_on_result()` | Every terminal state transition triggers an observation. |
| **1.2 KV Storage** | Implement persistence of `TaskObservation` into a dedicated NATS JetStream KV bucket. | `src/memnc/bus.py` | `bus.deposit_observation(obs)` | Observations are stored in the `mesh_observations` KV bucket (dots are invalid in KV bucket names). |
| **1.3 Event Emission** | Emit a NATS event upon successful deposit for UI/Console visibility. | `src/memnc/bus.py` | `bus.emit_event(obs, "observation.deposited")` | Real-time notification that learning has occurred. |
| **1.4 Test: Lifecycle** | Verify that task completion -> observation -> KV round-trip works without blocking task completion. | `tests/test_observations.py` | New integration tests | `test_observation_lifecycle_success` |

- **Type:** 💻 Pure Code / Test Work
- **Dependency:** Step 0 (Schema) must be green.

---

### Step 2: Trace Query (The "Read" Path)
**Goal:** Provide a way for humans to inspect the lived experience of the mesh.
**Status:** Planned.

| Task | Description | Targeted Files | Interface/Key Changes | Acceptance Criteria |
| :--- | :--- | :--- | :--- | :--- |
| **2.1 CLI Inspection** | Add `obslast` command to the `memnc` CLI to query the `mesh_observations` KV. | `src/memnc/cli.py` | `memnc obslast [--limit N]` | Returns a human-readable list of recent observations. |
| **2.2 Telegram View** | Extend `TelegramObserver` to support an `obslast` command. | `src/memnc/telegram.py` | New command handler | Telegram users can query recent mesh experiences. |
| **2.3 View Filtering** | Implement basic filtering by `task_type` or `role` in the query layer. | `src/memnc/bus.py` | `bus.list_observations(filter=...)` | Ability to see only "coder" or "critic" experiences. |

- **Type:** 💻 Pure Code / Test Work
- **Dependency:** Step 1 must be fully green.

---

### Step 3: Team-Synergy Aggregation (Measurement)
**Goal:** Surface metrics that quantify how well different model pairings/roles are performing.
**Status:** Planned (Crucial: No behavioral changes at this stage).

| Task | Description | Targeted Files | Interface/Key Changes | Acceptance Criteria |
| :--- | :--- | :--- | :--- | :--- |
| **3.1 Aggregation Engine** | Implement logic to compute success ratios and revision counts from stored observations. | `src/memnc/scoring.py` | `compute_synergy_metrics()` | Correct math for: `Success / Total` per model-pair. |
| **3.2 Console Dashboard** | Add a summary table to the `memnc` console showing model performance. | `src/memnc/console.py` | New `mesh stats` command | Live table of agent/model synergy metrics. |
| **3.3 Telegram Digest** | (Optional) Daily/periodic summary of mesh performance sent via Telegram. | `src/memnc/telegram.py` | Periodic scheduler | High-level overview of mesh drift or stability. |

- **Type:** 💻 Pure Code / Test Work
- **Dependency:** Step 2 must be complete.

---

## Parallel Tracks: Robustness & Model Intelligence

While the Experience Graph is being built, these tracks must run in parallel to ensure the system is stable enough for Step 4 (Behavioral change).

### Track A: Robustness (Hardening)
*Focus: Ensure the orchestrator/worker lifecycle is resilient.*
- **Worker Crash Handling:** How the mesh reacts to a worker dying mid-shell.
- **Task Requeue/Timeout:** Implementing the `MAX_REVISIONS` and standard task timeouts.
- **Orchestrator Statelessness:** Ensuring restarts via JetStream/KV allow for seamless recovery.

### Track B: Model Intelligence (Intelligence Layer)
*Focus: Optimizing the "brain" of the agents.*
- **JSON Contract Hardening:** Addressing the `llama3.2:3b` JSON output failures (strict schemas + retry logic).
- **Model Selection:** Deciding on the primary role-models (e.g., Gemma-4-31B for Critic).
- **Prompt Engineering for observations:** Refining what constitutes a "high-value" observation.

### Track C: Corpus-Derived Architecture Proposals (2026-11-10, from 25-paper corpus)
*Source: ~/research-papers synthesis + cross-reading. Top-3 by impact, ranked.*

1. **Implicit preference-mining from failed trajectories** (Implicit-Preference paper, arXiv 2603.17187).
   Preference pairs can be auto-mined from self-corrections inside failed agent trajectories.
   **Mapping:** the Step-1 `mesh_observations` KV bucket with `outcome: failed` is exactly this raw material — observations are *training data*, not telemetry. Wire a miner that extracts (rejected attempt, corrected attempt) pairs from `mesh_observations` and stages them as DPO/LoRA training pairs for the weight-memory pipeline.
2. **Per-role LoRA adapters instead of shared prompts** (arXiv 2608.09251).
   One shared adapter cannot represent planner- vs coder-updates that point in divergent directions in weight space — specialization is a mathematical constraint, not a taste choice.
   **Mapping:** replace "same model, different prompt" (llama3.2:3b as both planner and coder) with role-specific adapters as soon as the weight-memory pipeline lands. Single-model roles are a structural diversity killer in self-improvement loops (cf. Multiagent-FT, Yilun-Du papers: models collapse into their own patterns without diversity).
3. **Puppeteer pattern for tool-call JSON** (MLPO / Puppeteer / 2605.08626).
   3B models lack instruction-following depth for structured tool-args under load; this is not a prompt problem. Decouple reasoning from execution: a reasoning model decides *what* to do, a thin executor turns a fixed action set into deterministic JSON.
   **Mapping:** coder stays llama3.2:3b but only selects from a fixed action enum; the orchestrator (or critic side) builds the JSON tool-call. Cheap fix, no new model needed.

*Supporting context:* leader = meta-reasoner, not best solver (Gemma-31B critic fits; matters for future Apex). LoRA-memory vision gets its mathematical foundation from PE-MA (2506.11803: shared adapter for common knowledge + personal adapter per node, convergence guarantee) — the design target for Growing-LoRA. Small-base operating point legitimized by the MDPI edge paper (Qwen3-0.6B, 4.8× faster latency at acceptable reliability). Trust-Ledger (M8) grows in importance with swarm scale — large swarms fail on noise vs signal, not unwillingness to cooperate.

> **Sequencing note:** these proposals feed Step 4+ (behavioral change / weight memory). Do not pull them forward before Step 3 metrics exist — measure first.

---

## Implementation Sequence Summary

1.  **Phase 1 (Implementation):** Steps 1 $\rightarrow$ 2 $\rightarrow$ 3 (Sequential, data-driven).
2.  **Phase 2 (Validation):** Measure performance via Step 3.
3.  **Phase 3 (Behavioral Change):** Step 4 (Similarity) and Step 5 (Routing) — **Only after Step 3 validates current baseline.**

> **Rule of Thumb:** *Measure first, act later. No routing changes until Step 3 metrics are stable and meaningful.*
