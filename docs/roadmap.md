# Roadmap

## Built

- **2026-11-13 — §14.13 Step 5, experience-driven claim gate** (commits
  a8cd309, 9d118b6, 7636e00): workers aggregate their own per-server track
  record from deposited observations and withdraw from claiming when the
  ratio drops below 0.3 with >= 3 samples (fail-closed). Live on
  node-a + node-c.
- **2026-11-13 — Topology framework** (commit 5856c84): `NATS_URL` accepts
  comma-separated seed lists (client-side server pool / reconnect across
  masters), `NATS_KV_REPLICAS` setting wired into every KV bucket, nats
  compose entrypoint generates a Raft cluster block when
  `NATS_CLUSTER_ROUTES` is set (port 6222). Reference topologies (Solo /
  Duo cold-standby / Mesh >= 3 sites incl. witness) documented in
  `docs/operations.md`. Deployment topology is settings-only — the
  framework is not tied to any one machine layout.

## 14. Milestones

| ID | Deliverable | Acceptance |
|---|---|---|
| M0 | Repo scaffold, config, Dockerfiles, compose with NATS | `docker compose up nats` healthy; `meshctl doctor` connects |
| M1 | `schema.py` + `fsm.py` + tests | All transitions/guards covered; no I/O imports in `fsm.py` |
| M2 | `bus.py`, orchestrator, echo worker | Submit → PLANNING → CODING → CRITIQUE → DONE with no model |
| M3 | Inference backends + fake-server tests | Both providers parse tool calls; backoff verified |
| M4 | Role prompts + real planner/coder/critic | Real model produces plan, code, verdict; revision loop works |
| M5 | MCP server/client + tool loop + checkpoints | Coder edits workspace; redelivery does not re-run tools |
| M6 | Hardening + docs + smoke | Restart/recovery tests pass; README runs start-to-finish; `smoke.sh` green |
| M7 | Heterogeneous adapter + capability registry (# 14.1 Roadmap: symmetric 2-model self-orchestration ()

Goal: both nodes are **architecturally identical** — every node can submit, plan, claim, code, critique, and fan out. No fixed role, no "controller node". The orchestrator is gradually demoted from dispatcher to optional referee (M11 direction, completed in M14).

| Step | Content | Done criterion |
|---|---|---|
| **M12 — M11 live, both nodes** | Rebuild Node A with M11 code; bring Node B up to the current state (`docker-compose.node.yml`); `mesh_claim=true` on Node-B workers; live test: `meshctl submit --claimable` → Node-B worker claims via CAS and runs the pipeline | Task submitted on A, claimed+DONE by B; second run in reverse (B submits → A claims) | **[DONE ]** |
| **M13 — Agent as live submitter** | Coder/planner agent calls `mesh_submit` from inside a running task; the subtask lands in the mesh and is claimed by the *other* node | Proof: task on A creates a subtask, B works it off (and vice versa); `meshctl ledger` shows both nodes as submitter+claimer | **[DONE ]** |
| **M14 — Orchestrator-less dispatch** | Claimable tasks get `next_role`; only the matching role can claim (CAS role gate); the claiming worker continues the pipeline **itself** (reuses FSM `transition`) and makes the next step claimable (RECEIVED→PLANNING→CODING→CRITIQUE→DONE). Orchestrator remains a pure spectator; `_dispatch_claimed` optional | Submit with the orchestrator stopped: task is still claimed and executed; orchestrator restart only aggregates | **[CODE DONE , commit b49e142: next_role field, role-gated CAS claim, worker starts the pipeline itself (reuses FSM `transition`), orchestrator skips worker-driven tasks; 124 unit tests green. LIVE TEST DONE: claimable run (task 1a1cf0) submitted, orchestrator stopped immediately after accept; workers ran the full pipeline themselves (planner -> coder -> critic -> DONE) and every claim wrote its submitter-signed attribution entry to the trust ledger. Earlier claimable smoke runs with the orchestrator up also green (referee spectator role confirmed)]** |
| **M15 — Symmetric topology** | Node B runs its own orchestrator (responsible only for its own submissions/fan-out); one broker (NATS A) or leaf nodes; both nodes config-identical except models | Both nodes submit fan-out tasks simultaneously without collision (CAS/KV protects); docs §3 updated | **[CODE DONE /03, commits 86fc593 + 928e2ed: owner-routed submit subjects, origin_node, ownership guards, worker-side fanout split, Node-B orchestrator service, wildcard submit subscription + per-node durable names + legacy node-a acceptance; LIVE TEST STILL OPEN: simultaneous fan-out run of both nodes]** |
| **M16 — Demo: real peer orchestration** | A task is placed on B, B plans, fan-out lands symmetrically on A+B, both models (different Ollamas) work each other's shares, cross-node critique | End-to-end run with alternating direction ×2 (A→B and B→A shares), `task.done` with bidirectional attribution in the ledger |

Non-blockers (optional afterwards): broker JWT/ACL hardening (M9 remainder), raise `MAX_TOOL_STEPS` for the 3B coder, trust utility in the claim decision (M8 score at claim poll).

#
## 14.2 Expansion stage: smoke gate, UI, human steering ()

*Status: S1 is implemented and green (`39be633`). S2–S4 are pure planning — nothing built yet. The outlook sections 14.3/14.4 are also vision, not a roadmap with dates.*

This stage comes **after** M16 (real peer orchestration live) and turns the mesh into a daily-usable tool. Order is deliberate: first the gate (S1), then the models (S2), then visibility (S3), then control (S4).

| Step | Content | Done criterion |
|---|---|---|
| **S1 — Smoke gate** | Make `scripts/smoke.sh` hard: submit → poll `state` until DONE/FAILED with timeout (instead of a blind `tail --limit`); assert on end state DONE, exit code 0/1 (CI-capable); modes `--claimable` (referee mode: orchestrator accepts but does not distribute — workers with `MESH_CLAIM=true` claim on their own = M14 proof) and `--fanout`; runnable against both the Ollama AND the OpenAI backend (`INFERENCE_PROVIDER` parameter) | `./scripts/smoke.sh` ends green on both nodes; red exit when the task FAILED/times out; smoke reused for the M14/M15/M16 live tests |

**S1 result ():** green (pipeline PLANNING→CODING→CRITIQUE→DONE in 39 s, commit `39be633`). Observations: mannix tool-schema jitter (4/7 calls with wrong argument names), critic laxity (`hello from memnc` instead of `hello from the mesh` passed) — both S2/S4-relevant. On node-b the gate correctly went red: llama3.2:3b attempted 6/7 tool calls with wrong argument nesting (`p:` instead of `pattern:`, `f:` instead of `path:`) and wrote a 6-byte-truncated hello.py → FAILED/exit 1 (gate behavior correct); qwen2.5-coder:7b failed as planner ("no JSON object found"), while a direct Ollama probe with a minimal prompt immediately produced perfect JSON → the problem is the long planner prompt/context situation on the CPU box, not the model in general. **M14 `--claimable` live: pending** — submitting works (task lands RECEIVED/claimable), but no claim happens because `MESH_CLAIM=false` on all workers of both nodes; next step: `MESH_CLAIM=true` on the node-b worker compose, repeat the smoke test with `--claimable`, document the claim transitions (claimed_by/node-b) as evidence.
| **S2 — Swift as default backend** | Worker compose to `INFERENCE_PROVIDER=openai`, `INFERENCE_BASE_URL=http://host.docker.internal:8097/v1`, `MODEL_*` to Swift-1.5 (Qwen3.8-27B GSQ, llama-server :8097); the 27b coder gets offloaded when VRAM is contended (Swift ~13 GB + coder ~16 GB do not fit together); mannix stays the reserve model for discussion rounds | Smoke (S1) green with Swift; `meshctl doctor` shows Swift latency; no thinking leaks (Swift thinks briefly, the num_ctx budget holds) |
| **S3 — UI (mesh console)** | Local web console (small HTTP server, no framework obligation): task list + state colors, ledger view (registry/grants/trust), live `tail` (SSE or polling), node roster with load/latency; read-only — writes nothing | `http://localhost:<port>` shows live the same state as `meshctl ledger`/`tasks`; works from both nodes (Node-B renders against its own broker) | — **IMPLEMENTED :** `src/memnc/console.py` (stdlib `ThreadingHTTPServer`, polling frontend 3 s, tasks/caps/grants/trust/ledger, task-detail snapshot via `/api/task/<id>`; `python -m memnc.console --port 8790`)
| **S4 — Human-in-the-loop (steering)** | Approval gate in the critique step: task parks in CRITIQUE with the `needs_human=true` flag (KV), console/Telegram shows diff+verdict, human approves/rejects → FSM continues; Telegram bot as the second interface (submit, approve, tail); grants remain the authorization layer (M9) | End-to-end: task with a criticism-worthy patch parks, human rejects → revision loop, approves → DONE; everything operable from Telegram too |

Scope note: S4 replaces neither the M9 ACLs (machine layer) nor the trust scores (statistical layer) — the human veto is the third layer on top.

#
## 14.3 Outlook: self-organization & social competence ()

Two core elements carry the mesh concept beyond DeepSeed's dsh-superagent (whose panel shows only "what needs a person" — the human in the loop; our agents additionally organize *among themselves*):

**Self-organization** — the mesh distributes itself, no central dispatcher:
- *already real:* cross-node CAS claiming (M11/M12), agent subtasks across hierarchy levels (M13), owner routing + fan-out (M15)
- *expansion:* **dynamic roles** — the discussion (disc_20261103_230643) concluded that fixed leader-follower roles create deadlocks under async latency; state-based coordination + capability hierarchy instead. **Trust score in the claim decision** (M8 score at claim poll): reliable nodes automatically inherit more attractive tasks — reputation-driven self-organization.

**Social competence** — negotiation instead of command execution:
- *model↔model:* peer discussion with substantive objection and self-termination ([DONE], disc_20261103_230643) as a demonstrable negotiation pattern; critique step as institutionalized peer review; registry/capabilities as a social protocol ("who can do what")
- *model↔human:* trust ledger as social memory, grants as the authority instance (M9), approval gate (S4) as human veto
- *expansion:* task-driven consensus finding — critical tasks may trigger a model debate before execution (the discussion harness becomes a mesh primitive); verdict recommendations flow into the trust score.

**Design consequence for S3:** the console is the observation window of self-organization, not a dashboard: it shows *who organizes how* (claims, roles, node roster) and *where the negotiation stands* (critique verdicts, trust, grants) — not just a task list.

**Self-organization ladder** (, prompted by "how flexible should
role assignment be?"; grounded in SwarmHarness 2605.28764 as the origin
paper). A deliberately ordered path — each step only pays off once the
mesh is actually heterogeneous (different hardware/models per node):

- **Stage 0 — already real.** Cross-node CAS claiming (M11/M12), owner
  routing + fan-out (M15), critique debate as institutionalized peer
  review. The mesh already distributes itself; only roles are static.
- **Stage 1 — trust-weighted claiming** (near-free, do first). The
  SwarmHarness utility score `U = w1·cap + w2·(1−load) + w3·(1−lat) +
  w4·trust` applied at the claim decision. The trust ledger (T7
  reputation) and the claim gate already exist; only the weighting at
  claim-poll time is missing. Effect: reliable nodes automatically
  inherit more attractive tasks — reputation-driven self-organization
  with no central dispatcher.
- **Stage 2 — capability registry + dynamic roles** (the real target
  state for the heterogeneous vision). Instead of a fixed `roles=coder`
  in the roster, nodes *advertise* skills — including models on disk
  (already available via Ollama `/api/tags`) — and tasks ask "who can
  do X?" instead of targeting queue groups. One node can then act as
  planner *and* coder depending on which model is loaded. The
   discussion (disc_20261103_230643) warned correctly: fixed
  leader-follower roles deadlock under async latency — state-based
  coordination plus a capability hierarchy is the way out.
- **Stage 3 — emergence / stigmergy** (design direction, not a
  milestone). Ormica-style: routing and trust signals as digital
  pheromones, roles arising from demand rather than config. Honest
  caveat: with 3 nodes running local 3B–31B models, coordination
  overhead (negotiation rounds cost GPU time) is real, and every
  non-deterministic distribution step makes debugging harder. The
  paper itself gates this: utility routing pays off only once nodes
  *differ* — heterogeneity raises the value, homogeneity makes it
  noise.

Guardrail: none of this weakens the human gate — self-organization
distributes *work*; approvals stay human. That is the clean split.
And per 
## 14.4 Outlook: task affinity, evidence metrics & learned distribution ()

**Task affinity / "don't take the task away"** (from the DeepSeek dsh-superagent prototype, S., ):
- Once a task is distributed to a model, it should not be immediately taken away again — models tend to ("I can do this better"), but redistribution is inefficient: the receive context (prefill, history, workspace state) must be rebuilt. DeepSeek solves this via a **long-term KV cache**; our counterpart: task stability at the workflow level.
- *Status:* partially in — `origin_node` routing (M15) binds tasks to the owner node, claims are hard (CAS), critique loops stay with the same worker. **Not in:** affinity at the model level. Once cloud models enter the mesh (S2 variant with an external API), cache hits become a first-order cost/latency factor: redistribution = full prefill = full token payment; staying on the model instance = cache hit.
- *Expansion:* routing considers `(node, model_id)` affinity — resumed tasks/follow-ups prefer the instance that already holds the context in cache. Hot-standby instances per role (Swift/Ollama) as cache carriers.

**Evidence metrics for self-organization** (idea F., ; reference Graphify-Labs/graphify):
- Self-organization is unsteerable without measurement. We need an efficiency benchmark: **naive tool-call loop vs. structured search** (graph/retrieval-assisted) on identical tasks — measured by: tokens/task, **failed-tool-call ratio** (in the S1 run: 4/7 mannix calls faulty), time-to-DONE, critique round trips.
- Adopt the Graphify benchmark pattern: fixed agent, fixed questions on a real codebase (e.g. memnc itself; the ~1M-LOC ERPNext as benchmark reference: grep baseline 70.8% vs. graph 82.0% key-fact coverage), exact graders instead of an LLM judge. Independent replications warn: the structure advantage concentrates on multi-hop queries; grep wins simple lookups — so the mesh metric must measure **per task type**, not as a single number.
- Feeds a mesh primitive: task context (plan, workspace state, relevant code graph) as a queried resource instead of a re-read context.

**Self-learning distribution (ant-based):**
- Currently: static claim polling + owner routing. Later: distribution as stigmergy — tasks leave pheromone-like traces (success/cost signals per `(node, model, task_type)`), trust score + affinity are continuously updated from them instead of configured. Ant algorithm as the scoring scheme for the claim race; the already-measured evidence metrics are the starting signal function.

**Role fine-tuning (LoRA):**
- Fine-tune models with LoRA on social interaction and role behavior (planner/coder/critic as behavior styles, not just prompt roles) — fits the memory vision (Growing-LoRA, Fisher-Merge): the mesh roles become trainable model states; the critic learns from S1-like runs when strictness is justified (see critic laxity in the first S1 run).

#
§14.4 evidence metrics).
- **Collaboration formation with acceptance criteria:** before distribution, responsibility boundaries and explicit acceptance criteria are defined per subtask. Maps directly onto our critique loop: make the DONE definition machine-checkable instead of a benevolent critic.
- **Execution coordination as shared state:** dependencies, supervisory relationships and review feedback update a common state instead of message ping-pong — our ledger is already built this way; extend it into explicit dependency edges between subtasks.
- **Adapter idea:** AutoSwarm orchestrates foreign harnesses (Codex, Claude Code, Hermes, ...) via their native CLIs. Our mesh is currently homogeneous (own harness on both nodes). Later conceivable: dock foreign harnesses as worker adapters — for that, the ledger/claim API must suffice as an adapter interface.


#
## 14.6 Expansion stage: Telegram channel + console expansion — visibility, intervention, human-in-the-loop ()

*Trigger:* the M14 live test succeeded (task `1a1ad38d` → DONE rev=0 without orchestrator; the Gemma-31B critic verified itself via the MCP shell). Self-organization works — but it is invisible: you have to poll `meshctl state` yourself and grep through log files. The tests were laborious; the mesh needs a window through which Frank can **follow along, intervene, and steer**. Models: grok.bot / dots — a bot interface as the primary interface to the mesh.

*Relationship to S3/S4 (
§14.2):* S3 (mesh console, read-only, port 8790) is implemented. This stage concretizes S4 and builds on it — same order: first see, then intervene, then steer. No new framework, small steps, each individually testable.

| Step | Content | Done criterion |
|---|---|---|
| **T0 — E2E automation** | `scripts/smoke.sh` (S1) covers submit+poll+assert, but not the live-test effort: stack orchestration (start the orchestrator for submit → stop → let workers claim), timeout env (CRITIC/INFERENCE), stuck detection (state unchanged >N min → FAIL instead of polling forever), workspace reset, artifact verification (actually execute the produced script, check its output against the spec) and a one-liner PASS/FAIL with the critic verdict as evidence. Goal: an M14/M15/M16 live test = one command | `./scripts/e2e.sh --selforg "..."` runs the complete self-org live test autonomously and ends with PASS/FAIL + evidence; also runs in CI |
| **T1 — Telegram observer (read-only)** | Telegram bot (own service, `memnc.telegram`) that subscribes to task events from the NATS ledger and posts a message per transition: task ID, state, node/role, rev#, critic verdict (short). Plus `state <id>` and `tasks` responses to commands. No intervention — only a mirror of the ledger events | Frank sees in Telegram live the same progress as `meshctl tail`; an M14-style pipeline run is followable without polling |
| **T2 — Telegram intervention (steering inputs)** | Write commands, all authorized via grants (M9): `submit <task>`, `cancel <id>`, `retry <id>`, `stop worker <name>`; every action is recorded in the ledger as a human event (who, when, what) | Task submission and abort work from Telegram; actions are traceable in the ledger |
| **T3 — Approval gate (human-in-the-loop)** | Critique step can park: `needs_human=true` flag (KV) — Telegram shows plan/diff/verdict with approve/reject buttons; reject → revision loop, approve → DONE. Optional `--gate always|fail|never` per task | Criticism-worthy patch parks in CRITIQUE, Frank rejects from Telegram → revision → parks again → approve → DONE |
| **T4 — Console expansion (node/task management)** | Mesh console (S3) extended: node roster with live status (models, load, claim activity), task detail with trace/transcript, pause/resume per node, model routing visible and changeable. Read-only remains the base; write buttons only with a grant | The console shows the same live state from both nodes incl. the roster; one node can be paused and a worker takes over |
| **T5 — Discussion streaming (later)** | The model-model discussions (disc harness) and critique negotiations as a readable stream in console/Telegram — see the negotiation, not just the result | A critique debate is readable live in Telegram |
| **T6 — Node registry & resource-aware claim** | A mesh without the cloud must build its own discovery + allocation: (1) node roster over NATS — every worker advertises a heartbeat with capabilities (`node_id`, roles, model, RAM/VRAM, GPU type, load); (2) task envelopes get `needs` constraints (`needs: {vram_gb, model, roles}`); (3) the claim gate checks capability match before award — a critic task with a 20-GB model only lands on the node with enough VRAM; (4) roster as a read model for console (T4) and Telegram (T1). No distributed shared memory — the ledger remains the shared memory; tasks carry their context portably in the envelope (the snapshot of plan+verdicts travels with the task) | `meshctl roster` shows live the capabilities of both nodes; a task with `needs.vram_gb: 20` is claimed only by the suitable node, the other one skips it |
| **T7 — Model auto-load with human approval** | A worker recognizes from a task that a suitable model is missing locally (capability gap against the roster, T6) → proposes a `model_request` event over the ledger (model ID, size, purpose, source) → **Frank approves/rejects from Telegram** (approval gate, T3 mechanics at the model level) → after approve: `ollama pull` + roster entry + grant update → the model joins the claim market. AgentLance (arXiv 2608.23867, "Markets, Not Planners") as the model: reputation from critic verdicts/verify gates weights the claim (success is rewarded), bad models lose awards; retirement (`model_retire`) likewise only via human approval. No automatic download without the gate, no model sources outside a configured whitelist | A task requires a missing model → Telegram request → approve → pull runs → model claims tasks → after repeated critic rejections the mesh proposes retirement, Frank confirms |

Deliberately left out: no cloud dependency (the bot runs locally, only the Telegram API goes out), no second auth world — grants remain the only authorization layer; Telegram user binding (only Frank) is configuration, not a separate system.

*Note from the AgentLance reading (T7 design principle):* the authors themselves concede the main weakness of their market — agents often misestimate their bids (private costs) and bid suboptimally; the paper's fixes (reputation update, strategy notes) mitigate this but do not remove it. For memnc this means: our claim weighting must never be blindly guessed from model context — bids/reputation draw only on hard numbers kept in the ledger (measured task runtimes, critic-score history, verify-gate results). No self-declared capabilities in the roster; capabilities are measured, not claimed.

#
## 14.6.1 Status ()

*Status ():* T0, T1, T6, T7.5, T2, T3, T4 and T5 shipped. T5 (commit `ac9f10c`, discussion streaming): every coder/critic result now emits a readable `debate.code` / `debate.critique` event and records it into the task snapshot history — implemented as a shared `emit_debate()` helper in `bus.py` used by both the orchestrator (`_handle_result`) and the worker self-pipeline (M14). `meshctl trace <task-id>` and the Telegram `/trace <task-id>` command replay that history (`format_trace` in telegram.py is shared by both); the console task detail renders a "Debate trace" section under the raw snapshot. Live-verified end to end: a submitted task ran coder → critic, `meshctl trace` showed `debate.code` (summary + files) and `debate.critique` (APPROVED verdict + summary). 203 tests green (was 194). T4's task-detail-with-trace is now covered; model-routing-visible remains open. T7, T8 open. T4 (console expansion, first slice): the console now shows a **Nodes** table (roster live status: roles, models, VRAM/GPU, paused flag, last heartbeat) and forwards human control commands via `POST /api/control` — `pause_node`/`resume_node` for a node and `cancel`/`retry` for a task, always acting as agent `console` (the orchestrator grant-checks that id, so the buttons answer with a deny until `meshctl grant console` is issued). `meshctl pause <node>` / `meshctl resume <node>` do the same from the CLI. A paused node stops claiming work (the `paused` flag rides the roster entry, survives heartbeat merges, and is cleared by `resume_node` or a node restart); other nodes keep claiming its tasks. Task-detail-with-trace and model-routing-visible remain open parts of T4 scope. 194 tests green. T5, T7, T8 open. T3 (approval gate): tasks carry `gate: always|fail|never` (default `never` = legacy behavior); when the critic result reaches CRITIQUE, the FSM parks the task (`needs_human=true`, `task.parked` event) instead of finishing/revisioning whenever the gate applies (`always` = park on both verdicts, `fail` = park on rejection). A human decides via Telegram `/gate approve|reject <task-id>` or `meshctl gate approve|reject <task-id>` (both go through the same `mesh.control` `decide` action, grant-checked like all T2 commands): approve → DONE, reject → revision (exhausted revisions → FAILED). Results arriving for a parked task are ignored. Telegram `/approve`|`/reject` remain reserved for T7.5 model requests. T2 (commit `9e463a0` + `fae6b2c` + `46db138`): `ControlCommand` on subject `mesh.control`; orchestrator grant-checks each command (deny-by-default via `acl.check_registry_write`, CHAIN level) and records accepted actions as `human.submit` / `human.cancel` / `human.retry` events on the task snapshot. Telegram commands `/submit`, `/cancel`, `/retry`, `/stop worker <node>` forward via request-reply; response is JSON (`{"ok":true,"task_id":...}` or `{"error":...}`). The Telegram agent id is `telegram-<chat>` (KV keys reject `:`); a `CHAIN` grant for it must be issued once via `meshctl grant`. `stop_worker` withdraws all capability records on the named node — the process itself keeps running until the operator stops it on the host (per scope note above). Submit path is shared with meshctl submit (`_accept_submission`, incl. the M11 depth check).

#
## 14.7 Outlook: model demand planning & LAN model sharing ()

*Trigger:* Frank's idea — shuttle models back and forth between nodes so a model does not have to be loaded multiple times.

*Analysis:* two very different problems hide in this idea.

1. **Making model demand visible** (cheap, builds directly on T6): the roster already advertises which model each node has *loaded*. What is missing is what a node *could* load — models present on its local disk (Ollama blob store).
2. **Moving model bytes instead of re-downloading** (the interesting part): live VRAM migration is not feasible — llama.cpp/GGUF has no snapshot mechanism, a model must always be freshly loaded at the destination. The real saving is the download, not the load. With 20 GB GGUFs, a LAN transfer from a peer node beats an internet pull by orders of magnitude.

The practical win is therefore not shuttling but **demand-driven loading**: one model per node on disk, load/unload in VRAM on demand, coordinated through the roster.

| Step | Content | Done criterion |
|---|---|---|
| **T7.5 — Model demand planning** | The roster (T6) advertises `loaded_models` + `disk_models` per node. When a task's `needs.model` is unmet everywhere (no node has it loaded), the orchestrator checks disk_models: if a node has the model on disk, it emits a `model_request` event proposing *loading* (not pulling) — reusing the T7 approval gate: Frank approves from Telegram → node loads the model → roster updates → claim market serves the task. If no node has it on disk either, the request falls back to a `pull` proposal (T7 path) | A task needing model X, held on disk by node B but loaded nowhere, triggers one Telegram approval; after approve, node B loads X from disk and claims the task — zero network download |
| **T8 — LAN model sharing (optional, later)** | Node-to-node GGUF transfer for models not present on disk anywhere: an HTTP file server (or shared blob mount/rsync) lets a node fetch the Ollama blob from a peer over LAN before registering it. Only worth building once several nodes permanently hold different model sets | A model existing only on node A can be provisioned onto node B over LAN with human approval, without touching the internet |

Context: node-a currently holds three edge models (Gemma-4-31B-Humanlike-Chat as critic, Apex-35B-V1.0, ripple) — role assignment for Apex and ripple is still open and deliberately deferred until this stage is discussed.


#
## 14.8 Shipment: distribution & deployment

Two artifacts, two audiences (decided ):

- **Mesh services — docker compose (status quo, stays).** One `docker compose up -d` on a fresh host stands up NATS + orchestrator + workers + telegram observer. Workers need host-native Ollama anyway (ROCm), so containers never carry inference. Image is `memnc:latest`, built from the same `pyproject.toml`; the only node-specific input is env (`.env`: token, allow-list, VRAM/GPU, NATS URL). No registry dependency for now — `compose build` on the target node is the delivery.
- **`meshctl` — installable package (uv/pip).** Client-side only: hosts and agents (Hermes, LAN admins) must not run Docker to talk to the mesh. The package already exists (`pyproject.toml`, Python 3.11+); ship it as `uv tool install memnc` (or `uv sync` from a clone). Client needs no token for a plaintext NATS; with auth, `NATS_URL` carries credentials via query params (`nats://user:token@host:4222`) — document that.

Explicitly NOT shipped as a uv package: orchestrator/worker/observer runtimes. They are stateful-in-practice (Ollama co-location, GPU env, container isolation for the shell sandbox — see §15) and packaging them for pip would duplicate the compose path for no gain. If a later phase wants bare-metal workers, that is a separate `memnc-worker` package decision, not now.

Checklist for shipping a new node:
1. `git clone` + `docker compose up -d --build` (or pull prebuilt image later).
2. Node env: `NODE_NAME`, `NODE_ROLES`, `NODE_VRAM_GB`, `NODE_GPU`, `OLLAMA_BASE_URL`, `TELEGRAM_*` only on the observer host.
3. Install meshctl on the operator/agent host: `uv tool install` from the repo.
4. `meshctl doctor` → `meshctl roster` to verify presence.

#
## 14.9 Hermes plugin as second approval surface (PLANNED, not built)

Plan only — deliberately not implemented yet (Frank, ).

A small Hermes-side plugin/skill wrapping `meshctl` as a second approval
channel next to Telegram (
§14.7/T7.5). Both channels are first-come
first-served on the same KV record + `mesh.model.decision` stream; the
second one sees "already decided". Scope when built:

- skill with the `mreqs` / `decide approve|reject` workflow (meshctl from
  the host, `NATS_URL=nats://localhost:4222` override, PYTHONPATH caveat);
- optionally thin shell scripts so agents call `meshctl-mreqs` /
  `meshctl-decide` instead of remembering env flags;
- the Telegram observer keeps its minimal approve/reject write path
  unchanged; the observer stays read-only apart from that.

as the reference for the eventual in-repo plugin. Nothing of this ships
with memnc until Frank green-lights it.

#
## 14.12 Graph-based experiential memory & stigmergic routing (PLANNED, )

Origin: Frank's existing work in `eclipse-score/mcp-servers`
(S-CORE APM monorepo) — the design is **already built there**, the mesh
task is to port/adapt it, not to invent it. Verified against the repo:

**What exists in eclipse-score/mcp-servers (verified ):**

- `libs/score-context` (MCP-free, Apache-2.0, pydantic): the normalized
  write unit is a `ContextDelta` = one `Signal` (type, source, weight,
  **`decays: bool`** — the pheromone primitive is already in the schema)
  + `graph_ops` (upsert_node/upsert_edge). Every node/edge carries
  `Provenance` (repo, sha, adapter, **confidence**, observed_at).
- `packages/context-discipline` — the working-memory MCP server:
  session trace types are already defined — `SessionRecord`,
  `TaskRecord`, `ReasoningRecord`, `RetrievalRecord`, `OutcomeRecord`
  (pass/fail + coverage), `AttentionRecord`. Tools:
  `initialize_session`, `query_graph`, `record_decision`,
  `record_outcome`, `get_prior_context` (intersession retrieval),
  `add_overlay_node`. Sessions AND tasks are traced — session-to-session
  learning is live there.
- `context_attention.py` — deterministic ranked edge attention:
  `score_candidate` combines semantic (jaccard over tokenized goals),
  structural (grounded-node overlap), **recency decay**
  (`0.5 ** (age_days / half_life_days)`), **outcome reward/bonus**
  (pass/fail reinforcement), and **corroboration** (multiple independent
  records agreeing → reward). Selection uses `threshold` or `top_k`
  with `noise_floor` + `rank_gap_ratio`, so stale records fall out of
  attention *automatically* without being deleted from the log
  (decay-as-pruning, not decay-as-deletion).

**Design principle (Frank):** every model deposits its own experience
as observations in the graph; other agents *follow those traces* on
similar tasks and reinforce them. Ant-based attention scales this up
once the graph grows. The graph is shared experiential memory
(stigmergy), not a context-retrieval tool.

**memnc mapping (PLANNED):**

1. **Observation deposit.** After each result, the worker emits a
   `ContextDelta`-shaped observation: task type, role, model, route
   (already recorded since T4-Rest), plan steps, critique summary,
   outcome. The T7 reputation *scalar* becomes a derived/aggregated
   view over graph edges — not the storage itself.
2. **Trace-following at claim time.** The claim decision attends over
   ranked edges from similar past tasks ("this task type ran well on
   node X with model Y, critic needed 1 round") instead of a
   configured utility score — weights emerge from use (Stage 1+ of the
   
§14.4
Graphify-pattern metrics) — with the score engine we get a baseline
comparator for free: run grep-baseline vs. graph-attention on identical
tasks before trusting the routing.

**Distributed graph topology (Frank's design intent).** The mesh does not
maintain one monolithic graph: each node produces its own graph — its
experience observations per model (from task traces) plus a graphify code
graph of the repo it works on. Graphs are merged selectively
(cf. `context_merge.py` in eclipse-score/mcp-servers). **God nodes**
(graphify: nodes with a disproportionate number of edges) serve the mesh
twice: (a) in the code graph, a god node marks high blast-radius targets —
tasks touching one earn extra critic attention or a more capable model
(risk-aware routing, the same escalate-only signal AE uses); (b) in the
merged experience graph, a god node reveals where knowledge concentrates
("everyone's traces point here") — a bootstrap answer for a new node and a
signal for context prefetch on similar tasks.

**Model-team synergy in the graph (Frank, ).** Which models
work well *together* is itself an observation: every task implicitly
runs a team (the `route` dict maps roles to `node:model`). Each finished
task deposits a cooperation edge for every unordered model pair that
served on it, weighted by the outcome (done+approved = reinforce,
failed = decay). Over time the graph yields a team-performance view:
"task-type T ran well with planner A + coder B + critic C" — which
feeds back into routing (prefer proven teams for similar tasks) and
into pull/retire discussions. Pairs where one model dominated the route
(e.g. same model on two roles) produce no self-edge.

**Nightly graph pruning ("dreaming") (future).** A scheduled job (cron /
systemd timer, one node, human-approved design) walks the merged graph at
night: applies offline decay to stale edges, merges corroborated traces
into consolidated knowledge (observations → durable task-pattern nodes),
prunes contradicted or superseded edges (schema-version sweep), and
refreshes god-node rankings. Follows the dreaming pattern: consolidation,
not deletion — rejected/contradicted records stay archived in the log,
only their attention weight decays. Runs when no workers hold claims, so
it never races the CAS layer.

#
§14.12
vision. Each step is extend → test → harden → continue (Frank's rule);
no step starts before the previous one is fully green in the suite.

**Step 0 — DONE: observation schema (commit d6cb4fb).**
`TaskObservation` + `observation_from_snapshot()`, 13 tests, suite green.

**Step 1 — Deposit wiring (small, local, next).**
Orchestrator calls `observation_from_snapshot()` at every terminal
transition and persists the observation into a new KV bucket
(`mesh_observations` KV bucket — dots are invalid in KV bucket names —, subject prefix `mesh.obs.*`). Emit an event so
Telegram/console can confirm deposits. Tests: transition hooks, KV
round-trip, deposit does not break terminal-state handling on KV
failure (observation loss degrades learning, never the task).

**Step 2 — Trace query (read path before intelligence).**
Read observations back per task-type prefix and per model. Gives an
early human view (meshctl/Telegram command, e.g. `obslast`) so Frank
can sanity-check what the mesh remembers before any automatic use.

**Step 3 — Team-synergy aggregation (pure computation, no behavior).**
DONE for the CLI surface: `compute_synergy_metrics()` +
`format_synergy()` in `src/memnc/synergy.py` (success ratio, mean
revisions, sample size per unordered model pair), exposed as
`meshctl synergy [--limit N]` (verified live against the mesh KV).
Console dashboard + optional Telegram digest still open. No routing
decisions — measurement first (
§14.13 Steps 1–4; remaining: Step 4 similarity/ranked attention (Step 5 claim gate DONE, commits a8cd309/9d118b6) |
| B2 | Role-specific LoRA adapters | One adapter slot per role (planner/coder/critic) on a shared base; trained on TaskObservations from B1. **Training backend exists as a standalone vendored copy** (Unsloth QLoRA+EWC on Qwen2.5-7B, Fisher tooling, GGUF/Ollama hot-swap — proven loop from the memory project, 2026-09). B2 = data bridge (observations → per-role splits), not a new training stack. Dense LoRAs only: MoE-expert LoRAs are not servable on AMD/Vulkan. | §19 P0 (MoRSE) |
| B3 | Leader synthesis layer (MLPO pattern, no RL yet) | Orchestrator/critic prompt layer: leader receives all worker outputs with an explicit verify-and-backtrack instruction; difficulty filter skips routing tasks every available worker solves. Orchestration code, not training. | §18 + §19 P0 (MLPO, arXiv 2507.08960) |
| B4 | Fisher-Merge pipeline | mergekit-based merge of role adapters with a subspace-divergence check (principal angles) before merging; naive averaging is known to degrade (MoRSE result). | §19 P0 + synthesis.md §5 |

**PROTOTYPE ONLY (P1, experiment, no mesh commit until proven):**

| # | Item | Test design | Gate to build |
|---|---|---|---|
| T1 | Latent KV-communication (preprints_202609_1298) | One NATS message carries a KV-cache summary / hidden-state digest instead of text between coder→critic; measure critic quality delta. | Survey-level research, no protocol exists; only build a protocol if the delta is real |
| T2 | Neutral consensus + trust voting (04_majority_following) | Shuffle option labels, neutral phrasing, trust-weighted vote `w_i ∝ β_i` in the critique step; 2–3 rounds max (critical group size N_c: bigger swarms freeze in partial consensus). | Cheap S4-adjacent experiment; adopt if vote variance drops |
| T3 | CORY training loop (Harry67Hu/CORY, TRL-based) | Pioneer/observer pairs on the B2 role adapters, role exchange every 5 iterations, collective reward. | Only after B1 collected enough trajectories; pointless without a dataset |

**OBSERVE ONLY (P2, revisit later):**

- Incentive governance (2605_25815): complexity-weighted metrics against score
  inflation — relevant once workers self-evaluate or preen results. Plugin in
  `memnc/evaluation/`, not before.
- Growing-LoRA / rank growth (PE-MA shared+personal adapters, arXiv 2506.11803):
  the PE-MA mixture (μ·y_personal + (1−μ)·y_shared, ~50 lines) is adopted inside
  B2 from day one; actual rank growth waits until role adapters are stable.
  Personal adapter never leaves the node; only shared-adapter deltas cross the
  mesh (~87% less traffic than full-model sync).

**Brownfield status (verified ):**

- AgentNet (github.com/zoe-yyx/AgentNet, NeurIPS 2025): public code for the
  dynamically evolving DAG topology — use as reference for B1 data structures
  and update rules; map onto NATS KV, do not import the framework.
- CORY (github.com/Harry67Hu/CORY): official TRL-based implementation — the
  recipe for T3 when we get there.
- MLPO: no public repo found; B3 is our own implementation of the described
  pattern (GRPO + difficulty filtering + synthetic backtracking traces only if
  we later move to actual leader RL).
- LangGraph/AutoGen/CrewAI: NOT brownfield for us — they re-centralize
  orchestration as a graph interpreter, the opposite of the peer-to-peer NATS
  design. Anthropic's orchestrator-worker engineering write-up is a design
  reference for M16, not a dependency.

**Sequencing:** §14.13 Step 5 claim gate is DONE; next
Step 4 (similarity/ranked attention) → B3 leader
prompt layer → B2 role-LoRA training from observations → B4 merges. T1 runs as
a time-boxed side experiment; T2 rides along with S4; T3 after B2.



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
**Status:** 3.1 + CLI table DONE (`src/memnc/synergy.py`, `meshctl synergy`); console dashboard + Telegram digest open. (Crucial: No behavioral changes at this stage.)

| Task | Description | Targeted Files | Interface/Key Changes | Acceptance Criteria |
| :--- | :--- | :--- | :--- | :--- |
| **3.1 Aggregation Engine** | DONE: success ratio + mean revisions + sample size per unordered model pair. Lives in `src/memnc/synergy.py` (not `scoring.py` — kept the pure computation in its own module). | `src/memnc/synergy.py` | `compute_synergy_metrics()`, `format_synergy()` | 8 unit tests; math verified. |
| **3.2 CLI Table** | DONE: `meshctl synergy [--limit N]` (name follows the existing `obslast` verb style instead of the planned `mesh stats`). | `src/memnc/cli.py` | `meshctl synergy` | Live table verified against the mesh KV. |
| **3.2b Console Dashboard** | Add a summary table to the `memnc` console showing model performance. | `src/memnc/console.py` | reuses `format_synergy()` | Live table of agent/model synergy metrics. |
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

### Track C: Corpus-Derived Architecture Proposals (, from 25-paper corpus)
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


## Next
## 14.10 Console v2: live view + problem surface (PLANNED, deferred )

Frank's motivation: Telegram delivers events but no overview — he wants
updates pushed to him, visibility of *which node does what*, and problems
surfaced without polling commands. The T4 console slice already has the
static bones (node roster, task buttons, debate trace); v2 is about
liveness and problem focus:

- live updates without reload (SSE/WebSocket on `mesh.task.*.events` +
  roster changes),
- a per-node activity view (current claim, last result, heartbeat age),
- a **task decomposition timeline**: how the planner broke the task down
  (plan steps/subtasks, fanout tree), which role/model handled each
  step, state transitions with timestamps, and *when human-in-the-loop
  happened* (gate parked, who approved/rejected via which channel,
  model requests raised and decided),
- a "problems" surface: parked tasks, failed/retrying tasks, silent
  nodes (heartbeat stale), models nearing retirement threshold,
- optional notification rules ("park > 10 min" → Telegram DM) so the
  observer stays the push channel and the console the rich view.

Deferred deliberately — build after 
## 14.11 Usability & operator experience (PLANNED, researched )

Goal: make daily driving of the mesh feel like texting a competent
operator, not querying a database. Ideas researched against current
chatops/agent-UX practice (inline-keyboard approvals are the established
pattern; "actions in context" and edit-in-place confirmation are the
consensus UX rules). Ordered by impact; each item states what it fixes
today.

### P0 — friction we feel every day

1. **Inline keyboards for gates and control actions** (biggest single win).
   Instead of typing `/approve <request-id>` / `/gate approve <task-id>`,
   the bot sends the pending request as a message with buttons:
   `[✅ Approve] [❌ Reject]` for model requests, `[Cancel] [Retry]` under
   failed-task events, `[Pause] [Resume]` under roster anomalies. On tap:
   `answerCallbackQuery` + edit the original message in place to show the
   decision (buttons removed, decision stamped) — no extra chat clutter.
   Implementation: python-telegram-bot `CallbackQueryHandler`; store the
   `ControlCommand` as `callback_data` (compact `m:<req-id>:approve`,
   `t:<task-id>:retry`); the existing grant-checked `_control` path is
   reused unchanged, only the transport becomes a button.
   Fixes: copying 20-hex IDs on mobile, typo risk, slow approval loop.
2. **Short task IDs everywhere.** Accept unambiguous prefixes (≥6 chars)
   for every task-id argument (`state`, `trace`, `cancel`, `retry`,
   `gate`); `/tasks` and `/trace` already print only a prefix — align
   them. Fixes: ID copy/paste round trips.
3. **`/status` one-shot digest.** One command answering "is everything
   fine?": tasks by state (count + the interesting ones), nodes with
   heartbeat age and current claim, pending model requests, models near
   retirement. Replaces composing `/tasks` + `/roster` + `/mreqs` +
   `/mrep`. The console's roster data model is the source; the digest is
   a formatter, not a new data path.
4. **Failure-first notifications with the cause.** Today a failed task
   arrives as one event line among many; the reason ("invalid coder
   output") hides in the trace. When a task reaches a terminal state,
   push a short verdict card: what failed, which role/model ran it, the
   last critic objection, and *what to do* (`/retry <id>` or, if the
   route shows the weak model, a one-tap "pull a stronger coder" button
   that files the ModelRequest). Fixes: silent failure discovery,
   model-mismatch invisible → remediation one tap away.

### P1 — smoother daily loop

5. **Task output delivered to chat.** On completion, push the result
   artifact (or its path/summary) with the final event, not just ledger
   state. Fix: no second tool hop to read what the mesh produced.
6. **Retry with intent.** `/retry <id> --model qwen2.5-coder:7b` (or a
   button variant) overrides the model for the rerun; `/retry` alone
   keeps the recorded route. Fixes: rerunning a task into the same
   failing configuration.
7. **Command discovery.** Register the command list with BotFather
   (setMyCommands) so Telegram autocompletes; `/help` prints the same
   list grouped (view / act / decide). Fix: the dense one-line usage
   string is the only discovery surface today.
8. **Quieter event stream.** Group and throttle routine state transitions
   (e.g. claim→advance collapsed), keep the loud channel for submit /
   failure / gate / completion. Rationale: notification rules belong to
   the user, not the emitter; 
§14.10's "park > 10 min → DM" rule then
   composes on top. Fix: event spam vs. signal (Frank's original
   Telegram frustration).

### P2 — polish, tie into later milestones

9. **Model-suggestion flow on failure**: when the failure classifier
   (robustness milestone) tags "weak model" and reputation data agrees,
   the failure card offers the known-good alternative from the roster's
   `disk_models`/whitelist. Bridges 
§14.13) come first because they produce the
training data everything below depends on. Repo stays English (Frank's rule);
extend → test → harden → continue.

**BUILD (P0, committed to the plan):**

| # | Item | What | Where |
|---|---|---|---|
| B1 | Experience-graph steps 1–4 (

## Later
## 14.13 Build-order roadmap for the experience graph ()

Dependency-ordered sequence from the current state to the full 
§14.12 point 4).

**Step 6 — Dreaming job (nightly consolidation).**
Scheduled offline walk: decay, consolidate corroborated traces,
archive contradicted edges, refresh god-node rankings. Human-approved
design first; optional proposal digest for human sign-off.

**Step 7 — Distributed graphs + god nodes (later).**
Per-node graph shards, selective merge, god-node risk hook in claims.

Explicitly deferred: embeddings, ant-based scaling, cross-node merges —
only after Steps 1–5 prove out on real task data.



## Miscellaneous
§14.4, the ordering discipline applies: **measure first**
(tokens/task, failed-tool-call ratio, critique round trips, per task
type per the Graphify benchmark pattern) before dynamizing — without
a baseline we cannot know whether flexible distribution beats the
static one.

#
## 14.5 Outlook: AutoSwarm — heterogeneous agent harnesses under one roof ()

Reference: [HKUDS/AutoSwarm](https://github.com/HKUDS/AutoSwarm) (arXiv 2610.33048, MIT) — not a replacement for our mesh design, but several concepts well suited for further expansion:

- **Capability-based agent selection instead of fixed roles:** AutoSwarm selects agents by capability profile (coding, research, browsing, ...), not by configuration role. Conceivable for memnc: the roster holds not only `(node, model)` but capability tags; the router matches task requirements against capabilities instead of static roles.
- **Shared context/memory across harness boundaries:** AutoSwarm's core thesis is "Unify your context" — agents have isolated contexts; the coordination layer shares context/memory. Our counterpart: the NATS ledger as a context bus. Outlook: task context (plan, workspace state, relevant graph excerpts) not as one big prompt but as a queried resource (connects to the 
§14.9 and once T4's remaining
model-routing-visible slice lands, since both share the task-detail
data model.

#
§14.11 → 
§14.10 problem surface.
10. **Console ↔ Telegram links**: task-detail deep links from failure
    cards into the console page (and back-buttons into chat). Zero new
    state; pure URL construction once 
§14.10 lands.
11. **Drafted-submit flow**: `/submit` with no argument starts a guided
    prompt (Bot API question mode); plain chat text stays non-commands
    (no accidental task creation). Fix: new-user error surface.

### Non-goals / guardrails

- No freeform chat-as-instruction: commands stay explicit and
  grant-checked; buttons only *transport* existing `ControlCommand`s —
  no new authority path (T2 deny-by-default model untouched).
- Single-consumer rule unchanged: buttons are handled by the same
  observer poller via callback queries, not a second getUpdates loop.
- German-language chat output stays a presentation-layer choice; repo
  strings stay English (hard repo constraint).

Build order: items 1–2 as one slice (buttons + short IDs share the
control plumbing), 3–4 as the second slice, 5–8 opportunistically.
Items 9–10 wait for the robustness classifier / 
§14.10 respectively.

### Verified against the live cluster ()

- Short IDs are a real gap: `meshctl state 1a1c28` → "task not found"
  (full 24-hex required today).
- Failure-card data already exists: `snapshot.error` is set on failure
  (orchestrator + worker paths), the reason is also in `history`
  ("invalid coder output: no JSON object found"), the critic verdict
  text is in `snapshot.critique`, and `route` names the model per step.
- `/status` digest data already exists: roster nodes carry `updated`
  heartbeat timestamps and `disk_models` (heartbeat age + model
  suggestions computable); `HistoryEntry.at` timestamps make the
  timeline possible without schema changes.
- The ledger's pile of FAILED tasks (incl. a planning 404
  "model not found" case) is itself the failure-first-notification
  use case: those were silent until someone ran `tasks`.

#
§14.3 ladder, organic rather than hand-tuned).
3. **Engine choice.** `libs/score-context` is MCP-free and independent —
   embed it directly (it already brings decay, reward, corroboration,
   provenance, deterministic selection) rather than re-implementing.
   memnc adds an adapter: worker/orchestrator events → `ContextDelta`.
4. **Two consumers of one signal.** The human-gated retirement decision
   keeps the hard T7 scalar (auditable numbers); the graph drives
   dynamic routing. Same origin, different consumers.
5. **KV charset constraint.** Graph records in the JetStream KV follow
   `[-_./a-zA-Z0-9]` — node/edge keys must use `reputation_key()`-style
   sanitization (`:` → `_`).
6. **Open questions.** (a) similarity basis for "similar task" — the
   score engine uses jaccard on goal text; embeddings are an upgrade
   path; (b) per-model locality: observations stay attributed per
   (node, model) via Provenance.adapter so heterogeneous meshes get a
   learned capability map instead of a hardcoded `cap` term; (c) schema
   versioning of observation payloads so old formats don't poison the
   ranking.

**Prerequisite discipline (unchanged):** measure first (
§14.4 guardrail).

**Step 4 — Similarity + ranked attention (first real graph step).**
Jaccard similarity over task text/type as the first basis (embedding
upgrade stays open, 
§14.12 open point (a)). Port the deterministic
ranked attention from `context_attention.py` (half-life decay + outcome
reward + corroboration) onto our observations. Still read-only.

**Step 5 — Routing feedback loop (behavior change, human-gated).**
Claim decision consults ranked traces alongside the existing configured
utility score; a config flag (default off) selects the mode. Compare
both modes on a shadow period before switching.
- **DONE (commits a8cd309, 9d118b6):** experience-driven claim gate —
  workers aggregate deposited observations per server ("node-role:model")
  via `compute_server_success()` (synergy.py) and withdraw from claiming
  when their own track record drops below `claim_synergy_min_ratio`
  (default 0.3, >= `claim_synergy_min_samples` (default 3) required;
  fail-closed: thin evidence never benches a server). No central
  scheduler — the node reads its own history (CLAIM_SYNERGY_GATE switch,
  on by default). First live observation: gate withdrew on 1/3 ratio at
  the initial 0.34 default -> default retuned to 0.3 (only 0/n benches). T7 scalar stays as the
human-gated retirement source (two consumers, 
§14.13) | Observation deposits, trace query, team-synergy aggregation, similarity + ranked attention. This IS the AgentNet edge layer on our KV ledger — no external graph DB needed. |
# Roadmap — prioritized (2026-11-13)

Inputs: 26-paper corpus (§19), insights from 2026-11-11/12 — MoRAM/CoDyRA
(growing-LoRA as memory), Fisher-Merge (mergekit), arXiv 2603.01097
(single-LoRA beats multi-LoRA), Swift-GGUF template bug (tool-loop Jinja;
fix: ollama-qwen3 template with lastUserIdx instead of broken
multi_step_tool), Mesh API concept M11 incl. Fabro prior art, OpenCode run
on the remote node (fail: template, not build mode).

Resources: cloud model (GLM, synthesis/review), node-a (local GPU rig,
llama.cpp + MTP), node-b (remote host, memnc stack live + ollama with
swift:latest / qwen2.5-coder:7b). Machine specifics stay in chat/memory,
never in this repo.

## P0 — M8 clearance: live router on NATS + signed attribution — DONE
Scorer + trust ledger existed (03f1f35). Shipped: router runs live in the
NATS fabric and writes submitter-signed attribution to the ledger at claim
time (CAS-win in bus.try_claim, lazy ensure_trust, ':' key sanitizer —
NATS KV rejects ':' and silently swallowed every credit before).
Done-criteria met live: meshctl roster shows routed claims, ledger entries
carry the signature.

## P1 — Model set + Swift verification — DONE
1. Swift template fix verified: 2-turn chat with tool continuation against
   the ollama API, Jinja error ("No user query found") gone.
2. Coder role: llama3.2:3b falls over on JSON; qwen2.5-coder:7b / Swift
   are the coder options. Smoke (S1) is the gate.
3. Critic stays Gemma-4-31B (settled).

## P2a — Live tests M14/M15/M16 (both nodes) — DONE
- M14 claimable live: workers claim without the orchestrator (hard proof:
  orchestrator stopped right after accept, full pipeline ran itself,
  submitter-signed attribution in the ledger).
- M15: simultaneous fan-out of both nodes — green (single pipeline fanned
  across node-a and node-b).
- M16: cross-node critique, two-sided attribution — green (node-b critic
  closed the task DONE).

## P2b — Parametric memory (research, design-first)
Growing-LoRA (MoRAM/CoDyRA line) instead of RAG: small base + large
LoRA rank ~ data volume, Fisher-Merge via mergekit as compression,
single-LoRA advantage (2603.01097) as working hypothesis.
- node-a: LoRA-rank experiment with the existing Unsloth/EWC pipeline
  (external training dir), train/valid divergence as overfitting measure.
- Cloud model: paper synthesis on MoRAM/CoDyRA into docs/plan.md
  (memory-vision section exists).
- Design doc first, then code. Blocks nothing, open-ended.

## P3 — M11 Mesh API rollout (M11a-d, concept done)
M11a reads + listen (JetStream): DONE (2026-11-12).
M11b writes + Telegram API client: DONE (2026-11-12) — WRITE_VERBS in
api.py forward every mutating call over mesh.control (orchestrator stays
single writer, grant-check + human.* ledger unchanged); Telegram
`/submit|/cancel|/retry|/gate|/stop worker|/pause|/resume` goes through
api_request now.
M11c console proxy + resources: DONE (2026-11-12) — the web console is an
API client too: collect_state/task detail/control POST go through
api_request (reads: task.list/node.list + new cap.list/grant.list/
trust.list/ledger.read verbs; fallback to direct Bus reads if the API
server is down); node load is part of the resource view.
Next: M11d NL agent. Channels/human-as-peer from 5a feed into S4
(approval gate) — S4 and M11b merge.

## P4 — Infra robustness (decided 2026-11-13)

Topology support is DONE (see Built). Open infra slices:

1. **NATS cold-standby backup (Duo topology)**: periodic KV/ledger backup
   on the active node (nats stream backup / KV export into the nats-data
   companion), documented promote/restore for the second host. Recovery
   point = time since last backup; acceptable for a 2-host setup (Raft
   majority impossible without a 3rd voter). Decision: this is the
   failover story until the mesh goes productive; a data-less witness
   (tiny VPS, ~30 MB RAM) is the upgrade path to real automatic failover
   (Mesh topology, replicas=3).
2. **Node update logic**: version check at heartbeat, refuse/upgrade
   stale nodes — motivated by today's manual rsync rollouts.
3. **Wake-on-backlog (WoL)**: mesh detects backlog, wakes the cold
   reserve host. Posture: cold reserve capacity, no replication overlap
   with slice 1.

## 14.14 Harness abstraction layer (pi first, BUILT 2026-11-14)

### Worker wiring (DONE 2026-11-14)

When ``harness_coder != "inproc"``, the worker's coder role routes through the
harness instead of the in-proc loop:

- ``Worker._run_impl`` dispatches ``Role.CODER`` + non-inproc harness to
  ``Worker._run_harness`` (worker.py). Non-coder roles are untouched.
- ``_run_harness`` builds the coder prompt, runs
  ``get_harness(harness_coder, model=...)`` against ``workspace_root`` with
  ``coder_timeout_s``, then harvests workspace state:
  ``harnesses.workspace.changed_file_contents`` (``git add -N .`` +
  ``git diff --name-only``, untracked files included, non-repo -> empty).
- Harvested files are syntax-checked (``python_syntax_errors``) and converted
  to ``CoderOutput(summary, files=FileArtifact...)`` -> ``ResultPayload(ok,
  output, tool_steps=harness tool_events)``. The orchestrator/critic contract
  is unchanged: harness results look exactly like in-proc coder results.
- Harness failure / timeout / exception -> ``ResultPayload(ok=False)`` with
  the harness error, measured attempts (3x retry) as usual. No routing hack.

Verified live: worker + PiHarness + qwen2.5:7b in a scratch git repo ->
ok=True, tool_steps=1, hello.txt harvested with exact content. (qwen2.5-coder:32b
still returns 0 tool events -> empty files -> the known template backlog item.)

### ToolLoopHarness (in-proc loop as a Harness, DONE 2026-11-14)

``harnesses/inproc.py`` wraps ``memnc.agent.run_agent`` so the built-in coder
loop satisfies the same Harness contract as pi:

- ``ToolLoopHarness(model, backend, tools=None, execute_tool=None,
  system_prompt=None, max_steps=8, raw_prompt=False)`` -- registered as
  ``"inproc"`` in the harness registry.
- ``run(prompt, workspace, timeout_s)``: builds the coder prompt exactly like
  the worker's in-proc path (``build_user_prompt(Role.CODER, {"task": ...})`` +
  ``SYSTEM_PROMPTS[Role.CODER]``), runs the tool loop under ``asyncio.wait_for``,
  parses ``CoderOutput``, applies artifacts into the workspace
  (``fsutil.apply_artifacts``) and returns the workspace git diff in
  ``HarnessResult``. Bad JSON / timeout / backend error -> ``ok=False``.
- ``workspace_diff(workspace)`` helper added to ``harnesses/workspace.py``
  (git add -N + diff, untracked included); ``pi.py._git_diff`` now reuses it.
- Probe CLI: ``python -m memnc.harnesses --harness inproc --model M`` builds a
  backend from settings (model overridden) -- the built-in loop is now probeable
  from the host without pi.

Verified live: ToolLoopHarness + qwen2.5:7b + scratch git repo -> ok=True,
CoderOutput parsed, hello.txt applied, full diff harvested.

Tests: tests/test_harnesses.py -- changed_file_contents (untracked + non-repo),
worker harvest, failure path, broken-python rejection, non-coder roles ignore
the harness (deterministic via monkeypatch, immune to test pollution), default
stays inproc.


Status: core built + proven end-to-end; worker wiring is the next slice.

The weak->strong proof (7b dirty pass -> produktiv fixer, 11/11 green in
`~/memnc-harness-wt`) showed the *product* of a coding agent is workspace
state (git diff + tests), not structured patches. That is the contract the
new `src/memnc/harnesses/` package formalizes; it sits above the inference
backends (memnc.backends abstracts *models*, harnesses abstract *how the
model works on a workspace*).

**Contract (built):**
- `HarnessResult` — ok, summary, diff, tool_events, exit_code, error.
- `Harness` protocol — `capabilities()` + `async run(prompt, workspace, timeout_s)`.
- Registry — `@register`, `get_harness(name, **kwargs)`, `known_harnesses()`.

**Adapters:**
- `PiHarness` (`harnesses/pi.py`) — spawns `pi -p --mode json --model <m>`
  with the workspace as cwd, parses the JSONL event stream (tolerant to
  noise/huge lines), returns the workspace diff (untracked files included
  via `git add -N`). Carries the proven pitfalls: process-group kill on
  timeout (orphans otherwise hold the stdout pipe open), `tool_events == 0`
  as the no-native-tool-calls signal (the 32b template issue), generous
  timeouts for weak models (7b parallel-tool-call retry loop).
- Probe CLI `python -m memnc.harnesses --workspace W --model M "prompt"`
  runs one prompt through a harness from the host and prints a JSON result.

**Config:** `harness_coder: str = "inproc"` in Settings (opt-in; "inproc"
keeps the worker's own tool loop as default).

**Tests:** `tests/test_harnesses.py` — event parsing (tools/summary/settled,
noise tolerance, error turns), registry selection, and subprocess behavior
(ok path, nonzero exit, timeout kills the whole tree <10s, missing binary)
using throwaway fake `pi` scripts.

**Next slices:**
1. Worker wiring: when `harness_coder == "pi"`, the coder role builds its
   usual prompt, runs the harness against a git worktree of the target
   workspace, and maps `HarnessResult` into the ResultPayload contract
   (diff -> evidence; Critic stays harness-agnostic).
2. `ToolLoopHarness`: wrap the worker's in-proc loop in the same protocol so
   routing sees one interface.
3. Per-task harness selection (instead of per-node config) — the step toward
   mesh self-organization: cheap models draft dirty, strong models fix.
4. `DiscussHarness` (multi-model debate) on the same contract.

## 14.15 Backlog: plan-level task queue with atomic claim (BUILT 2026-11-14)

User driver: "wir brauchen quasi einen Weg, Pläne auf Tasks runter zu
brechen und die zu blocken wie ein Lock so dass nicht andere damit
anfangen. Klassisches Scrum für Agents."

**Design:** one level above the mesh task queue (`bus.try_claim`, commit
a8cd309, KV-revision CAS as lock). `src/memnc/backlog.py`:

- `BacklogTask` model: `id` (`b-{uuid4}`), `plan_id` (groups the tasks
  decomposed from one plan), `prompt`, `status`
  (`planned`/`claimed`/`done`/`failed`), `attempts`, `claimed_at`,
  `claimant`, `mesh_task_id`, timestamps.
- `BacklogStore` on NATS KV bucket `memnc-backlog`. The lock is the KV
  revision: `claim()` reads the entry, flips `planned` -> `claimed` (or
  steals a stale claim), and writes back with
  `kv.update(key, value, last=entry.revision)`. On racing drivers exactly
  one CAS succeeds, the losers see a wrong-revision error and move on.
- Stale-claim reclaim: a `claimed` entry whose `claimed_at` is older than
  `CLAIM_STALE_S` (15 min) is stealable -- driver died mid-task and the
  work is not lost. Unparseable timestamps count as stale.
- `MAX_ATTEMPTS = 3` mirrors the mesh's own retry measurement; `failed`
  with attempts left goes back to `planned` for the next pass.

**meshctl subcommands:**
- `backlog-add "<prompt>" --plan-id <id>`: enqueue a task.
- `backlog-list`: planned/claimed/done/failed with attempts.
- `backlog-run [--once]`: the driver loop. Claim one entry, submit to the
  mesh (`TaskSubmission` claimable), mark `mesh_task_id`, watch to a
  terminal event, then `mark()` done/failed. Without `--once` it loops.
- `backlog-reset <id>`: re-plan an entry for debugging.

**Two live-bugs found and fixed during the smoke:**
1. Core-NATS subscribe misses fast tasks: the mesh terminal event can be
   published before the driver's subscription is live. Fix: the watcher
   uses a JetStream ordered consumer (`deliver_all`) on
   `mesh.task.{task_id}.events`, so it replays the retained history.
2. Terminal-event naming: worker/orchestrator emit
   `state.{snapshot.state.value.lower()}` on transitions, so the settled
   states arrive as `state.done`/`state.failed` -- not `task.done`/
   `task.failed` (those exist only in the orchestrator's own paths).

**Live proof (node-a, 2026-11-14):** add -> claim -> mesh task submitted
-> `state.failed` watched -> `failed` marked -> back to `planned`
(attempts 1/3) -> re-claimed twice -> attempts 3/3, entry terminal. Stale
claim of a killed driver was reclaimed by the next `--once` pass. Full
suite 372 passed + 2 skipped. Commit 22ef1a2.

**Next slices on this level:**
1. Plan decomposition helper: turn a plan doc into N `backlog-add` calls
   with a shared `plan_id` (the Scrum planning step).
2. Run the driver as a daemon (systemd unit / cron) so the machines
   consume work instead of idling (Auto-Driver stage 1).
3. Weak->strong routing rule on top of `attempts` (stage 2 of the
   orchestration design).

## Not pursuing
- CI maintenance (stopped by decision), Fabro clone (prior art
  suffices), HTTP mesh-to-mesh, federation v1.

### 14.15.1 Slice: plan-decomposition helper `backlog-plan` (BUILT 2026-11-15 night, commit 5378963)

`decompose_plan(text)` in `backlog.py` turns a written plan document into task prompts:
numbered (`1.`) or bullet (`- ` / `* `) lines win when >=2 exist, else blank-line-separated
paragraphs, else the whole text as one block.

`meshctl backlog-plan --plan-id <id> [--chain] [--dry-run] "<plan text>"` adds N backlog
entries sharing plan_id; `--chain` links each entry to its predecessor via `deps` so the
driver (`next_claimable`) consumes the chain in order. Gotcha: the FIRST entry must get
`deps=[]` — `deps=['']` (empty prev_id) makes the task unclaimable forever because no id
ever reaches `done`. Fixed in 5378963 (guard: `args.chain and prev_id`).

Live-proven: `night1` plan decomposed into 2 chained entries; the running driver daemon
claimed task 1 and submitted it into the mesh.

### 14.15.2 Infra findings (night 2026-11-15)

- All backlog attempts 404'd with `Client error '404 Not Found' for url 'http://host.docker.internal:11434/api/chat'` — root causes, both fixed/known:
  1. `docker-compose.node.yml` had no `extra_hosts: host.docker.internal:host-gateway` (root compose already did) — fixed in commit e8ccf47; node-c stack recreated with it. NOTE: the repo on node-c (`~/memnc`, branch feat/m8-live-router with local mods) is a separate checkout — the compose fix was copied over manually and the node-c repo needs a proper re-sync later.
  2. node-c's ollama does NOT have `qwen2.5:7b` (planner) nor `qwen2.5-coder:32b` (coder) — ollama answers 404 for unknown models, which looks identical to a network error in the agent error string. node-c only has `qwen2.5-coder:7b`, `llama3.2:3b`, `swift`, `gemma4`, and a 27B/35B MoE coder. FIX PENDING (needs user OK, .env edit on node-c blocked overnight): point node-c's MODEL_PLANNER/MODEL_CODER/MODEL_CRITIC at `qwen2.5-coder:7b` — weak node, weak models, exactly the weak->strong routing slice's premise (node-a has the strong models: qwen2.5:7b, qwen2.5-coder:32b, produktiv, signal-27b...). No model download needed.
- Design lesson for slice 3 (weak->strong routing): the 3x-fail -> FAILED measurement is exactly the signal the router needs — a task failing 3/3 on a weak node should be retried on a strong node before terminal failure, not just counted.
