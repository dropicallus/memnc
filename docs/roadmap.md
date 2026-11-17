# Roadmap

## Built

- **2026-11-16 — Worker-side strategy honor (14.15.10 selection consumption)**:
  the worker-driven pipeline now HONORS a non-default routing edge. When the
  orchestrator selected the self-reflect edge (`routing_edge == "B"`), the
  claimed worker skips the planner step (the raw task prompt IS the plan,
  `route.self_reflect` history event) and runs the arm-B loop in-process: the
  coder model critiques its own output via the normal critic role with
  `model_override = model_coder` (no critic claim, no extra round trip), the
  verdict flows through the same gates (acceptance, verify, reputation) as a
  claimed critic result, and the revision budget stays with the FSM
  (`max_revisions`, default 3 = bench arm B). A self-critique question parks
  for the human as usual. Default/other edges are untouched. Tests:
  `tests/test_worker_self_reflect.py` (5).

- **2026-11-16 — DiscussHarness (14.14 item 4)**: multi-model debate as a coder
  harness on the shared contract. Proposals are plain text (parallel, per-call
  timeouts), one critique round, judge returns `WINNER: <model>`, the winner is
  executed by the in-proc tool loop; every failure degrades instead of blocking.
  Config: `harness_coder="discuss"`, `discuss_models` (JSON list, empty ->
  `[model_coder]`), `judge_model`. Tests: `tests/test_harnesses_discuss.py`
  (phases, judge fallbacks, empty-models degenerate case, worker wiring).
  Design note: `docs/design-discuss-harness.md`.

- **2026-11-15 — Pheromone routing field (14.15.10, initialization slice)**:
  `src/memnc/pheromone.py` — ACO-style score tau^alpha * eta^beta per
  (task_type, edge) with tau0=1.0, alpha=1.0, beta=2.0 from the measured
  full run; seed deposits load the 24-cell rescue matrix (5 reps x 8 hard
  tasks x A/B/C) into the `mesh_pheromones` KV bucket; live terminal
  transitions deposit on the `mesh` edge via the orchestrator hook
  (best-effort, like observations); escalation chain A->B->C is the
  fail-floor (`choose_edge` never re-picks a failed edge); `meshctl
  pheromone [--task-type T]` renders the field. 14 tests; seeded + verified
  live on node-a.


- **2026-11-15 — Bench arm D, adversarial critic (15.3 item 3, D1)**: the
  critic must attempt a concrete refutation before any verdict; unevidenced
  approvals are rejected as critiques, loop capped at 2 revisions. Evidence
  gate unit-tested; bench run pending.

- **2026-11-15 — Structured message tuple (15.3 item 2)**: TalkHop-style
  communication tuple (message / background / intermediate_output) built
  end-to-end behind `Settings.tuple_prompts` (default off): schema models,
  persistent background as a workflow_state companion key, tuple-rendered
  prompts with prose fallback; no extra model calls.

- **2026-11-14 — Similarity + ranked attention (§14.13 Step 4)**: Jaccard
  similarity over task text ranks deposited observations against a free-text
  query; `ranked_attention` scores each match by similarity x half-life decay
  (default 14 days) x outcome reward (done 1.0, failed 0.25, cancelled drops)
  x corroboration (sufficiently-similar sibling observations boost the score,
  capped at 2x). Deterministic and read-only — no RNG, no wall-clock reads
  (`now` injectable), no ledger mutation. Surface: `meshctl similar "<query>"`
  or `--task-id <id>` (uses that observation's task as the query, excludes it
  from results), plus `--limit`/`--half-life`. Embedding upgrade stays open.
  Tests: `tests/test_experience.py` (18 cases).

- **2026-11-14 — Task output delivered to chat (P1 item 5)**: DONE events
  broadcast a completion card instead of a bare state line — `format_done_card`
  shows task title, the coder summary (400-char excerpt) and produced file
  paths, so the result itself lands in chat with the final event (no second
  tool hop to /trace). Tests: 3 cases in `tests/test_retry_intent.py`.

- **2026-11-14 — Command discovery (P1 item 7)**: the Telegram command list
  lives in one grouped table (`COMMAND_GROUPS`: view / act / decide) in
  `telegram.py`; unknown commands get the grouped list via `format_help()`
  (replacing the dense one-line usage string), and `BotAPI.set_my_commands`
  registers the flattened command list with BotFather at startup so Telegram
  autocompletes. Registration is best-effort (failure logged, never fatal).
  Tests: 3 cases in `tests/test_retry_intent.py`.

- **2026-11-17 — Model-suggestion flow on failure (P2 item 9)**:
  `Orchestrator._suggest_model` fires on the terminal FAILED transition
  (before the snapshot is stored, so the failure card sees it): failure
  classified via failclass; roster candidates for the failing role minus
  the failed model and retired ones, ranked by reputation approvals; pick
  recorded as `model.suggest`. `failure_suggestion()` in telegram.py
  renders it on the FAILED card. `tests/test_model_suggestion.py` (9
  cases: ranking, retired/paused/failed exclusion, unknown-failure
  no-op, card rendering).

- **2026-11-17 — Quieter event stream (P1 item 8)**: `Settings.
  telegram_quiet_progress` (env `TELEGRAM_QUIET_PROGRESS`, default on)
  makes the Telegram observer drop routine progress transitions
  (RECEIVED/CLAIMED/PLANNING/CODING/CRITIQUE/REVISE) from the digest; the
  New-task card, terminal states, WAIT_INPUT and failure detail rework
  still broadcast, `telegram_verbose` overrides. Tests in
  `tests/test_telegram_observer.py`.

- **2026-11-14 — Retry with intent (P1 item 6)**: `/retry <id> --model
  <name>` reruns a FAILED/CANCELLED task on a chosen model. `ControlCommand`
  gains `model_override`; the retry branch in the orchestrator stamps it on
  the snapshot (`TaskSnapshot.model_override`) and the re-dispatch payload
  carries it, so the worker's `_run_impl` uses it instead of the role
  default for every re-dispatch of that snapshot (plain retries keep the
  previously recorded override). Telegram parses the flag, the API write
  verb `task.retry` forwards it, and buttons still work unchanged. Tests:
  `tests/test_retry_intent.py` (7 cases: orchestrator stamping, payload
  forwarding, worker model choice, Telegram parsing).

- **2026-11-14 — Node revocation tombstones (§21.3)**: `meshctl
  revoke-node <id> --reason --by` writes a tombstone into the new KV bucket
  `mesh_revocations` (durable, included in cold-standby backup), withdraws
  every capability record of the node and drops its roster entry; `meshctl
  reinstate <id>` removes it. Enforcement while revoked: heartbeats are
  refused (roster gate), capability publishes raise `PermissionError`
  (capability gate), and every `try_claim` from that node is denied
  fail-closed before any CAS write — in-flight tasks finish untouched.
  `meshctl revocations` lists tombstones. Tests: `tests/test_revocation.py`
  (9 cases). Nothing in the mesh auto-revokes; revocation is a human cut.

- **2026-11-14 — Telegram inline keyboards + short task-ID prefixes
  (14.11 P0 slice 1)**: failure cards carry `[Retry][Cancel]` inline buttons;
  callback_data (`tk:/mr:/nd:` triplets) parses through a fixed verb table and
  every press rides the existing grant-checked `_control`/`_decide` API path
  (answerCallbackQuery + edit-in-place stamp, unauthorized chats only get the
  answer). `resolve_task_id()` accepts unambiguous ≥6-char prefixes for
  `state`/`trace`/`cancel`/`retry`/`gate`, with ambiguity and refusal
  messages. Tests: `tests/test_telegram_buttons.py` (25 cases).

- **2026-11-14 — Node code fingerprints (P4 slice 2)**: every worker
  advertises a deterministic code fingerprint (`memnc.version.code_version`,
  hash over installed package sources, `<__version__>+<hash12>`) in
  `NodeInfo.version`; `merge()` carries the freshest non-`dev` value. The
  orchestrator stamps `mesh_version` into every dispatch command; with
  `Settings.enforce_node_version` (opt-in, default off) a worker running
  different code naks the command so it stays available for compliant
  nodes. `meshctl doctor` lists the roster with per-node STALE marks and
  fails when any node runs other code. Unknown/`dev` versions fail open.

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

*Trigger:* the M14 live test succeeded (task `1a1ad38d` → DONE rev=0 without orchestrator; the Gemma-31B critic verified itself via the MCP shell). Self-organization works — but it is invisible: you have to poll `meshctl state` yourself and grep through log files. The tests were laborious; the mesh needs a window through which the operator can **follow along, intervene, and steer**. Models: grok.bot / dots — a bot interface as the primary interface to the mesh.

*Relationship to S3/S4 (
§14.2):* S3 (mesh console, read-only, port 8790) is implemented. This stage concretizes S4 and builds on it — same order: first see, then intervene, then steer. No new framework, small steps, each individually testable.

| Step | Content | Done criterion |
|---|---|---|
| **T0 — E2E automation** | `scripts/smoke.sh` (S1) covers submit+poll+assert, but not the live-test effort: stack orchestration (start the orchestrator for submit → stop → let workers claim), timeout env (CRITIC/INFERENCE), stuck detection (state unchanged >N min → FAIL instead of polling forever), workspace reset, artifact verification (actually execute the produced script, check its output against the spec) and a one-liner PASS/FAIL with the critic verdict as evidence. Goal: an M14/M15/M16 live test = one command | `./scripts/e2e.sh --selforg "..."` runs the complete self-org live test autonomously and ends with PASS/FAIL + evidence; also runs in CI |
| **T1 — Telegram observer (read-only)** | Telegram bot (own service, `memnc.telegram`) that subscribes to task events from the NATS ledger and posts a message per transition: task ID, state, node/role, rev#, critic verdict (short). Plus `state <id>` and `tasks` responses to commands. No intervention — only a mirror of the ledger events | the operator sees in Telegram live the same progress as `meshctl tail`; an M14-style pipeline run is followable without polling |
| **T2 — Telegram intervention (steering inputs)** | Write commands, all authorized via grants (M9): `submit <task>`, `cancel <id>`, `retry <id>`, `stop worker <name>`; every action is recorded in the ledger as a human event (who, when, what) | Task submission and abort work from Telegram; actions are traceable in the ledger |
| **T3 — Approval gate (human-in-the-loop)** | Critique step can park: `needs_human=true` flag (KV) — Telegram shows plan/diff/verdict with approve/reject buttons; reject → revision loop, approve → DONE. Optional `--gate always|fail|never` per task | Criticism-worthy patch parks in CRITIQUE, the operator rejects from Telegram → revision → parks again → approve → DONE |
| **T4 — Console expansion (node/task management)** | Mesh console (S3) extended: node roster with live status (models, load, claim activity), task detail with trace/transcript, pause/resume per node, model routing visible and changeable. Read-only remains the base; write buttons only with a grant | The console shows the same live state from both nodes incl. the roster; one node can be paused and a worker takes over |
| **T5 — Discussion streaming (later)** | The model-model discussions (disc harness) and critique negotiations as a readable stream in console/Telegram — see the negotiation, not just the result | A critique debate is readable live in Telegram |
| **T6 — Node registry & resource-aware claim** | A mesh without the cloud must build its own discovery + allocation: (1) node roster over NATS — every worker advertises a heartbeat with capabilities (`node_id`, roles, model, RAM/VRAM, GPU type, load); (2) task envelopes get `needs` constraints (`needs: {vram_gb, model, roles}`); (3) the claim gate checks capability match before award — a critic task with a 20-GB model only lands on the node with enough VRAM; (4) roster as a read model for console (T4) and Telegram (T1). No distributed shared memory — the ledger remains the shared memory; tasks carry their context portably in the envelope (the snapshot of plan+verdicts travels with the task) | `meshctl roster` shows live the capabilities of both nodes; a task with `needs.vram_gb: 20` is claimed only by the suitable node, the other one skips it |
| **T7 — Model auto-load with human approval** | A worker recognizes from a task that a suitable model is missing locally (capability gap against the roster, T6) → proposes a `model_request` event over the ledger (model ID, size, purpose, source) → **the operator approves/rejects from Telegram** (approval gate, T3 mechanics at the model level) → after approve: `ollama pull` + roster entry + grant update → the model joins the claim market. AgentLance (arXiv 2608.23867, "Markets, Not Planners") as the model: reputation from critic verdicts/verify gates weights the claim (success is rewarded), bad models lose awards; retirement (`model_retire`) likewise only via human approval. No automatic download without the gate, no model sources outside a configured whitelist | A task requires a missing model → Telegram request → approve → pull runs → model claims tasks → after repeated critic rejections the mesh proposes retirement, the operator confirms |

Deliberately left out: no cloud dependency (the bot runs locally, only the Telegram API goes out), no second auth world — grants remain the only authorization layer; Telegram user binding (only the operator) is configuration, not a separate system.

*Note from the AgentLance reading (T7 design principle):* the authors themselves concede the main weakness of their market — agents often misestimate their bids (private costs) and bid suboptimally; the paper's fixes (reputation update, strategy notes) mitigate this but do not remove it. For memnc this means: our claim weighting must never be blindly guessed from model context — bids/reputation draw only on hard numbers kept in the ledger (measured task runtimes, critic-score history, verify-gate results). No self-declared capabilities in the roster; capabilities are measured, not claimed.

#
## 14.6.1 Status ()

*Status ():* T0, T1, T6, T7.5, T2, T3, T4 and T5 shipped. T5 (commit `ac9f10c`, discussion streaming): every coder/critic result now emits a readable `debate.code` / `debate.critique` event and records it into the task snapshot history — implemented as a shared `emit_debate()` helper in `bus.py` used by both the orchestrator (`_handle_result`) and the worker self-pipeline (M14). `meshctl trace <task-id>` and the Telegram `/trace <task-id>` command replay that history (`format_trace` in telegram.py is shared by both); the console task detail renders a "Debate trace" section under the raw snapshot. Live-verified end to end: a submitted task ran coder → critic, `meshctl trace` showed `debate.code` (summary + files) and `debate.critique` (APPROVED verdict + summary). 203 tests green (was 194). T4's task-detail-with-trace is now covered; model-routing-visible remains open. T7, T8 open. T4 (console expansion, first slice): the console now shows a **Nodes** table (roster live status: roles, models, VRAM/GPU, paused flag, last heartbeat) and forwards human control commands via `POST /api/control` — `pause_node`/`resume_node` for a node and `cancel`/`retry` for a task, always acting as agent `console` (the orchestrator grant-checks that id, so the buttons answer with a deny until `meshctl grant console` is issued). `meshctl pause <node>` / `meshctl resume <node>` do the same from the CLI. A paused node stops claiming work (the `paused` flag rides the roster entry, survives heartbeat merges, and is cleared by `resume_node` or a node restart); other nodes keep claiming its tasks. Task-detail-with-trace and model-routing-visible remain open parts of T4 scope. 194 tests green. T5, T7, T8 open. T3 (approval gate): tasks carry `gate: always|fail|never` (default `never` = legacy behavior); when the critic result reaches CRITIQUE, the FSM parks the task (`needs_human=true`, `task.parked` event) instead of finishing/revisioning whenever the gate applies (`always` = park on both verdicts, `fail` = park on rejection). A human decides via Telegram `/gate approve|reject <task-id>` or `meshctl gate approve|reject <task-id>` (both go through the same `mesh.control` `decide` action, grant-checked like all T2 commands): approve → DONE, reject → revision (exhausted revisions → FAILED). Results arriving for a parked task are ignored. Telegram `/approve`|`/reject` remain reserved for T7.5 model requests. T2 (commit `9e463a0` + `fae6b2c` + `46db138`): `ControlCommand` on subject `mesh.control`; orchestrator grant-checks each command (deny-by-default via `acl.check_registry_write`, CHAIN level) and records accepted actions as `human.submit` / `human.cancel` / `human.retry` events on the task snapshot. Telegram commands `/submit`, `/cancel`, `/retry`, `/stop worker <node>` forward via request-reply; response is JSON (`{"ok":true,"task_id":...}` or `{"error":...}`). The Telegram agent id is `telegram-<chat>` (KV keys reject `:`); a `CHAIN` grant for it must be issued once via `meshctl grant`. `stop_worker` withdraws all capability records on the named node — the process itself keeps running until the operator stops it on the host (per scope note above). Submit path is shared with meshctl submit (`_accept_submission`, incl. the M11 depth check).

#
## 14.7 Outlook: model demand planning & LAN model sharing ()

*Trigger:* Design idea — shuttle models back and forth between nodes so a model does not have to be loaded multiple times.

*Analysis:* two very different problems hide in this idea.

1. **Making model demand visible** (cheap, builds directly on T6): the roster already advertises which model each node has *loaded*. What is missing is what a node *could* load — models present on its local disk (Ollama blob store).
2. **Moving model bytes instead of re-downloading** (the interesting part): live VRAM migration is not feasible — llama.cpp/GGUF has no snapshot mechanism, a model must always be freshly loaded at the destination. The real saving is the download, not the load. With 20 GB GGUFs, a LAN transfer from a peer node beats an internet pull by orders of magnitude.

The practical win is therefore not shuttling but **demand-driven loading**: one model per node on disk, load/unload in VRAM on demand, coordinated through the roster.

| Step | Content | Done criterion |
|---|---|---|
| **T7.5 — Model demand planning** | The roster (T6) advertises `loaded_models` + `disk_models` per node. When a task's `needs.model` is unmet everywhere (no node has it loaded), the orchestrator checks disk_models: if a node has the model on disk, it emits a `model_request` event proposing *loading* (not pulling) — reusing the T7 approval gate: the operator approves from Telegram → node loads the model → roster updates → claim market serves the task. If no node has it on disk either, the request falls back to a `pull` proposal (T7 path) | A task needing model X, held on disk by node B but loaded nowhere, triggers one Telegram approval; after approve, node B loads X from disk and claims the task — zero network download |
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

Plan only — deliberately not implemented yet (the operator, ).

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
with memnc until the operator green-lights it.

#
## 14.12 Graph-based experiential memory & stigmergic routing (PLANNED, )

Origin: Prior work in `eclipse-score/mcp-servers`
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

**Design principle (the operator):** every model deposits its own experience
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

**Distributed graph topology (the operator's design intent).** The mesh does not
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

**Model-team synergy in the graph (the operator, ).** Which models
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
vision. Each step is extend → test → harden → continue (the operator's rule);
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
early human view (meshctl/Telegram command, e.g. `obslast`) so the operator
can sanity-check what the mesh remembers before any automatic use.

**Step 3 — Team-synergy aggregation (pure computation, no behavior).**
DONE for the CLI surface: `compute_synergy_metrics()` +
`format_synergy()` in `src/memnc/synergy.py` (success ratio, mean
revisions, sample size per unordered model pair), exposed as
`meshctl synergy [--limit N]` (verified live against the mesh KV).
Console dashboard + optional Telegram digest still open. No routing
decisions — measurement first (
§14.13 Steps 1–4 all DONE (Step 4 similarity/ranked attention: `experience.py`, commit below); console dashboard + Telegram digest DONE (2026-11-17 night-builder: read verb `obs.synergy` in `api.py`, console "Synergy (model pairs)" table via the standard API-client/fallback path, Telegram `/synergy [limit]` reusing `format_synergy`) |
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

the operator's motivation: Telegram delivers events but no overview — he wants
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

1. **Inline keyboards for gates and control actions** — DONE (slice 1,
   shipped). `CallbackQueryHandler`-equivalent polling: failure cards carry
   `[Retry][Cancel]` (`tk:<id>:retry|cancel`), model-request buttons
   (`mr:<req-id>:approve|reject`) and node buttons (`nd:<node>:pause|resume`)
   parse through a fixed verb table; every press still rides the grant-checked
   `_control`/`_decide` API path (no new authority path), unauthorized chats
   only get the callback answered. Model-request/node cards are handled but
   not yet auto-announced — pending-request push is a later slice.
   Fixes: copying 20-hex IDs on mobile, typo risk, slow approval loop.
2. **Short task IDs everywhere** — DONE (slice 1). `resolve_task_id()`
   accepts unambiguous prefixes (≥6 chars, exact full ids always win) for
   `state`, `trace`, `cancel`, `retry`, `gate`; ambiguity lists the candidate
   prefixes, short/unknown prefixes say why. Fixes: ID copy/paste round trips.
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

5. **~~Task output delivered to chat.~~ DONE 2026-11-14** — see Built entry;
   DONE events now broadcast a completion card carrying summary + files.
6. **~~Retry with intent.~~ DONE 2026-11-14** — `/retry <id> --model <name>`
   overrides the model for the rerun; `/retry` alone keeps the recorded
   route. See the Built entry below.
7. **~~Command discovery.~~ DONE 2026-11-14** — see Built entry; grouped
   `/help` and BotFather `setMyCommands` registration at startup.
8. **~~Quieter event stream.~~ DONE 2026-11-17** — `TELEGRAM_QUIET_PROGRESS`
   (default on) drops routine progress transitions (RECEIVED/CLAIMED/
   PLANNING/CODING/CRITIQUE/REVISE) from the digest entirely; the New-task
   card, terminal states, WAIT_INPUT and failure details still broadcast.
   `telegram_verbose` still overrides to the full stream.

### P2 — polish, tie into later milestones

9. **~~Model-suggestion flow on failure.~~ DONE 2026-11-17** — on a
   terminal model-level failure (failclass: timeout / server /
   invalid_output) the orchestrator records a `model.suggest` history
   entry: the best alternative for the failing role from the live roster,
   ranked by reputation approvals, retired models skipped, advisory only.
   The Telegram failure card appends it as `next try: <role>: <model>`.
   Built entry below; tests in `tests/test_model_suggestion.py`.

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
- **DONE (2026-11-14):** Jaccard similarity over task text (embedding
  upgrade stays open, §14.12 open point (a)). Deterministic ranked
  attention ported onto the observation ledger in `experience.py`:
  score = similarity x half-life decay (default 14 days) x outcome reward
  (done 1.0, failed 0.25, cancelled drops) x corroboration (similar
  sibling observations boost, capped 2x). Read-only, no RNG, injectable
  clock. Surface: `meshctl similar "<query>"` / `--task-id <id>`.
  Tests: `tests/test_experience.py`.

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

### P2b counter-position (15.3 item 8): the argument against MAAI C2

The thesis to defend: parametric memory — interaction knowledge is stored IN
the weights (small base + growing-LoRA, rank ~ data volume; Fisher-Merge via
mergekit as compression), not retrieved. The named, peer-reviewed
counter-position is MAAI C2 (Electronic Markets 2026, item 12 above): the
data-centric doctrine — model weights fixed, ALL adaptation happens through
data and context. A second counter-position from the same corpus: MEM1
(ICLR 2026) — RL-trained state compression keeps working memory constant
over long interactions without any weight change. P2b is argued against
these, not against a RAG strawman.

Honest framing: this repo already RUNS the C2-orthodox stack as its
production path — KV ledger, observation deposits, pheromone routing, the
manual Erfahrungsspeicher. The bet is not "C2 is wrong"; it is that C2's
doctrine has a measurable boundary condition, and P2b claims durable
interaction memory sits outside it.

What C2 concedes implicitly: its doctrine assumes the data can be re-fed at
will. Offline-capable deployments, mesh nodes without shared storage, and
long-horizon agents pay a per-session transport/latency cost for re-feeding
context that weights would amortize once. P2b only claims this narrow
territory: durable interaction memory. Routing, orchestration, and ephemeral
state stay data-centric in memnc REGARDLESS of how P2b resolves — the two
doctrines are complementary layers, not competitors for the whole system.

Falsification test (bench discipline, not narrative): the LoRA-rank
experiment in the external training pipeline on a fixed interaction corpus.
Metrics: (a) train/valid divergence as the overfitting threshold — if
divergence dominates before the rank grows enough to pay, growing-LoRA is
dead for this data volume; (b) held-out recall of earlier interactions vs a
fixed-weights + KV-retrieval baseline at matched inference cost. Acceptance:
growing-LoRA recovers earlier-session knowledge measurably better than the
C2 baseline at comparable cost. Rejection: no recall gain, or the gain
exists only because the KV baseline was implemented badly. The dual-store
design (KV pheromones + weights) is the hedge: if P2b loses, the C2 path is
already built; if it wins, Fisher-Merge gives the compression step.

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
2. **Node update logic**: DONE — deterministic code fingerprints
   (`memnc.version`), orchestrator stamps `mesh_version` into dispatch,
   workers refuse mismatched code when `enforce_node_version` is on
   (opt-in until the fleet rolls it), `meshctl doctor` reports stale
   roster nodes.
3. **Wake-on-backlog (WoL)**: DONE — see section 14.15.8.

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


Status: core built + proven end-to-end; worker wiring, per-task selection, and
the DiscussHarness are built (see above).

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

### Per-task harness selection (slice 3, DONE 2026-11-14)

The harness is now selectable per task, not only per node, so one mesh can
mix cheap dirty drafts and strong fixes:

- `TaskSubmission.harness` ("" = node default) -> `TaskSnapshot.harness`.
  The orchestrator's claim payload carries it (`"harness": snapshot.harness`),
  as does the worker-driven self-pipeline payload.
- `Worker._run_impl` resolves the coder harness as
  `envelope.payload.get("harness") or settings.harness_coder`; `""`/missing
  falls back to the node default, and a payload `harness == "inproc"` forces
  the built-in loop even on a node configured for an external harness.
- `meshctl submit --harness <name>` sets it on a direct submission;
  `meshctl backlog-add --harness` and `meshctl backlog-plan --harness`
  stamp every generated backlog entry, and the backlog driver forwards
  `claimed.harness` into its `TaskSubmission` (strong retries preserve it
  via `make_strong_retry`).
- Tests: payload override wins over the node default, payload `inproc`
  bypasses the node's external harness, empty payload falls back, backlog
  roundtrip + claim + strong-retry carry the field.

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
3. ~~Per-task harness selection (instead of per-node config)~~ DONE (see
   "Per-task harness selection" above) — the step toward
   mesh self-organization: cheap models draft dirty, strong models fix.
4. ~~`DiscussHarness` (multi-model debate) on the same contract.~~ DONE —
   `harnesses/discuss.py` + `settings.discuss_models`/`judge_model`; design
   note in `docs/design-discuss-harness.md`. Debate object is the plan: K
   text proposals, one critique round, judge picks a WINNER line, winner is
   executed by the in-proc tool loop (same contract, same tools). Degrades:
   failed judge/garbage winner -> first valid proposal; zero proposals ->
   ok=False. Cost caveat: 2K+1 extra inference calls vs one for inproc.

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
- `backlog-del <id> [--force]`: KV tombstone; refuses claimed entries
  without `--force` (driver mid-task protection).

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

### 14.15.3 Slice: weak->strong routing via backlog attempts (BUILT 2026-11-15 night, commits 16ee63c + c370a03)

Slice 3 from the "Next slices" list. Mechanism, no new subjects and no
--needs-model hack:

- `TaskSubmission.prefer_node` / `TaskSnapshot.prefer_node` ("" = unconstrained).
- The orchestrator copies it into every dispatch command payload.
- A worker whose `node_name` differs naks the command (2 s delay) so a worker
  on the preferred node picks it up — targeting rides the existing role
  command queue (shared JetStream durable).
- The backlog driver: when `mark()` leaves a task terminally failed
  (attempts >= MAX_ATTEMPTS=3, `mark()` re-plans while budget remains), the
  driver queues ONE `[strong-retry]` copy (same prompt/plan_id/deps,
  `strong_retry=True`, `prefer_node=STRONG_NODE`). `should_strong_retry()`
  guards the loop: strong-retries never spawn further retries.
- `Settings.strong_node` (env `STRONG_NODE`, default "") disables the path.
- Gotcha (c370a03): worker node_names are role-suffixed (node-a-planner,
  node-a-coder, ...). A task dispatches to several roles, so exact-match
  targeting can never satisfy every dispatch — `prefer_node` names the HOST
  and workers match by prefix (`node_name.startswith(prefer_node)`).

Semantics: a weak node burns its 3 attempts on the weak pool first — that
measured failure is the routing signal — then the work item migrates to the
strong node exactly once.

### 14.15.5 Slice: failed-chain semantics (BUILT 2026-11-15 night)

When `mark()` drives a task terminally `failed` (attempt budget exhausted,
after weak pool and strong retry), the store cascade-fails every planned
task that transitively depends on it (`BacklogStore.fail_dependents`). A
dependent whose dep can never reach `done` would deadlock `next_claimable`
forever. Rules:

- only `planned` dependents are cascade-failed; claimed ones are in flight
  and terminate (and cascade) on their own;
- cascade-failed tasks keep `attempts` at 0 (they never ran);
- non-terminal failures (back to `planned` for the next retry) never
  cascade;
- the decision is fail-the-chain, not release-the-chain: a strong retry of
  the failed root is a separate entry and does NOT revive the failed
  dependents -- re-planning a chain is a manual `backlog-reset` decision.

### 14.15.4 Slice: daemonize the backlog driver (BUILT 2026-11-15 night)

Slice 2 from the "Next slices" list. The driver loop itself already exists
(`meshctl backlog-run`); the slice replaces the fragile nohup-from-/tmp
invocation with a repo-shipped service:

- `scripts/backlog-driver.sh`: launcher that encodes the client-env rules —
  never `source .env` (extract `NATS_TOKEN` with the plain-match awk pattern),
  host-side clients use `nats://127.0.0.1:4222` (the .env URL is the
  compose-internal hostname), empty tokens are never exported, PYTHONPATH is
  stripped, and it `exec`s `.venv/bin/meshctl backlog-run`.
- `scripts/systemd/memnc-backlog-driver.service`: systemd **user** unit
  (no root anywhere), `Restart=on-failure` + `RestartSec=10`. Killing the
  driver loses nothing — stale claims are reclaimed by the next pass.
- Install (per host): `cp scripts/systemd/memnc-backlog-driver.service
  ~/.config/systemd/user/ && systemctl --user daemon-reload && systemctl
  --user enable --now memnc-backlog-driver`. Requires lingering for
  boot-time start (`loginctl enable-linger`), otherwise it runs while the
  user session is up.
- `STRONG_NODE` passes through the environment (14.15.3 weak->strong
  routing), e.g. `Environment=STRONG_NODE=node-a` in the unit override.
- Tests: `tests/test_backlog_daemon.py` pins the invariants (executable
  launcher, no `source .env`, guarded token export, loopback default,
  PYTHONPATH strip, Restart policy, no root).

Deployment note: node-a swapped from the nohup driver to the user unit.

### 14.15.8 Slice: wake-on-backlog / WOL (BUILT 2026-11-14 night)

Slice 3 of the P4 list. The backlog driver wakes the cold-reserve host
when planned work is waiting but the mesh is otherwise idle:

- `src/memnc/wol.py`: `parse_mac` / `magic_packet` (6x0xFF + 16x MAC),
  `send_wol` (broadcast UDP datagram, SO_BROADCAST), and `WolWaker` —
  a throttled wake decision (`maybe_wake()` with a configurable clock
  and sender so tests inject fakes).
- Settings: `wol_mac` (env `WOL_MAC`, "" = disabled) and
  `wol_min_interval_s` (default 300). The idle loop cannot flood a
  sleeping host: at most one packet per interval.
- `meshctl backlog-run` calls `_maybe_wake_idle()` on every idle pass —
  a packet is sent only when planned backlog entries exist AND the waker
  is enabled. `meshctl wol [--mac M]` is the manual one-shot.
- Tests: `tests/test_wol.py` (MAC parsing variants, packet structure,
  broadcast send via a fake socket, throttle semantics, driver-hook
  gating on planned entries, CLI subparser wiring).

Cold-reserve posture: capacity wakes on demand, no replication overlap
with the cold-standby backup (slice 1).

### 14.15.6 prefer_node binding on the claim path (2026-11-14)

The strong-retry live proof exposed a gap: `prefer_node` was only enforced on
the command path (worker `_handle`), but claimable tasks run through the M14
worker-driven pipeline (`_claim_loop`), which ignored it entirely — node-c's
planner won the claim race for `[strong-retry]` tasks despite
`prefer_node=node-a`. Additionally the synergy gate could withdraw the
preferred node itself, deadlocking explicitly routed work.

Fix (`Worker._claim_permitted`): a snapshot with `prefer_node` set may only be
claimed by the preferred host (prefix match against the role-suffixed
node_name), and an explicit preference overrides the synergy-gate withdraw.
Tests in `tests/test_prefer_claim.py`; suite 408 passed, 2 skipped.

### 14.15.7 Robust role-output parsing (2026-11-14)

Live proof exposed a second parsing gap: a coder model emits its JSON tool
call (name/arguments) as *plain text* before the result object, and
`extract_json` returned the first object — the tool-call echo, which pydantic
validated into an *empty* CoderOutput (extra keys ignored). Every debate round
logged "files: no files" even though the model had written the file JSON.

Fix (agent.py):
- `extract_json_candidates` collects ALL JSON values in document order
  (fenced blocks first, raw scan skips past decoded values).
- `parse_role_output` validates every candidate, skips tool-call echoes
  (name+arguments), and picks the richest valid payload (files/commands/
  issues/plan/summary scored); ties go to document order. No candidate /
  nothing valid -> precise ValueError or first ValidationError.

Tests: tests/test_role_parse.py (6). Suite 414 passed, 2 skipped.
A debug INFO log (`raw role output`) in the worker keeps tool_steps and the
first 400 chars of model content visible for future parsing regressions.

### 14.15.8 Backlog watcher terminal events (2026-11-14)

The live proof then showed the orchestrator retrying a task to DONE while the
backlog driver had already marked it failed: `_backlog_watch` counted
`state.failed` (emitted on every FSM transition, once per debate attempt) as
terminal. Only `task.*` events are final now; 3 tests
(tests/test_backlog_watch.py). Suite 417 passed, 2 skipped.

### 14.15.12 Slice: backlog dependency resolution across mesh task ids (BUILT 2026-11-16)

Observed live (2026-11-16): 4 planned entries, all blocked forever on deps
that reference MESH task ids (`1a1e...`) which ended terminal `failed` —
even when the work itself succeeded later via backlog strong-retry. Driver
idle for hours. Design: docs/design-backlog-dep-resolution.md.

Mechanism: `next_claimable(claimant, dep_checker=...)` — the driver passes
`_mesh_dep_checker(settings)` (cli.py), which snapshots the mesh registry
once per call and reports a dep satisfied when the mesh task is in a
terminal state (done/failed/cancelled). Ordering intent, not a success
gate: the dependent task's own attempt budget + ship-gate stay the quality
controls. Rules:
- dep is a backlog entry that is `done` -> satisfied (unchanged);
- dep starts with `b-` but is not done -> BLOCKED even with a checker
  (the checker is for mesh ids only; unknown backlog ids never unblock);
- dep is a mesh id -> satisfied iff the registry shows a terminal state;
- unknown id (neither store) -> blocked, conservative.

Test double gotcha: the `_FakeKV` in tests/test_backlog_deps.py must carry
the full KV contract (get with `revision`, CAS `update(key, value, last)`,
`keys()`) — claim() swallows any exception as a CAS conflict, so an
incomplete fake silently makes `next_claimable` return None.

### 14.15.9 Slice: swarm-vs-single benchmark (PLANNED 2026-11-14)

Goal: measure whether the mesh actually beats a single model. Literature
context: debate/multi-agent gains are unreliable (Du et al. 2023; "If MAD is
the Answer...?" 2025; "Talk Isn't Always Cheap" 2025 — sycophantic flip of
correct answers; "More Agents Is All You Need" — gains correlate with task
difficulty). Heterogeneity is the factor that wins consistently. Current
system evidence: 40 live tasks, only 2 revisions — the critic approves
almost everything on proof-of-concept tasks (verify-gate override,
worker.py ~1114).

Design — three arms on the same task set:
  A) single strong: qwen2.5-coder:32b, one chance, no critic.
  B) single + self-reflection: 32b with 3 self-correction rounds
     (the necessary baseline a critic loop must beat).
  C) mesh: planner qwen2.5:7b -> coder 32b -> critic-loop (current debate).

Task set: 20-30 tasks with automatic verification (deterministic checks,
no LLM judge), difficulty tiers easy/medium/hard.

Runs: >= 5 seeded repetitions per task per arm. Metrics: success rate
(primary), revision rounds to success, tokens / GPU-seconds, wallclock.
Fair compute comparison: MAD vs. voting at equal token budget.

Implementation: a `bench` runner driving the three arms through the
worker/inproc path, writing `bench-report.md` with success rates and
confidence intervals. Nightly runs over the backlog driver (no extra GPU
cost). Backlog items: bench task set, bench runner + report, first
report run.

Status (2026-11-14): stage 1 done — `memnc.bench` package:
  - `tasks.py`: 24 tasks (8 easy / 8 medium / 8 hard), pure stdlib,
    deterministic, no network. Prompt names the produced file(s).
  - `checkers.py`: one deterministic checker per task (plain asserts,
    no LLM judge). Probes target the prompt's hard cases (LRU eviction
    order, lexicographic tie-break in Dijkstra, token-bucket refill cap
    and non-monotonic timestamps, csv quoting, mutation-freedom, etc.).
  - `solutions.py`: reference solutions; every checker is proven to
    accept them and reject a stub (tests/test_bench.py, 10 tests).
  Checker protocol: `check_<task_id>(workdir) -> list[str]`; empty list
  = pass. `verify_task()` is the single entry point for the runner.

Status (2026-11-14): stage 2 done — `memnc.bench.runner`:
  - Three arms over one inproc engine (`BenchEngine`) driving the same
    memnc backends with the SAME role prompts (production mesh has no
    solo mode, so the runner owns its engine): A single-shot coder,
    B coder + up to 3 self-critique rounds, C planner->coder->critic
    loop (max 3 revisions), mirroring the production FSM.
  - Checker isolation: deterministic checkers are NEVER shown to the
    models and never terminate a loop early; they score the final
    workspace once (checker leakage would turn arm C into "arm A plus
    an oracle").
  - Seeded reps via a fixed temperature ladder (deterministic, no RNG;
    identical prompts, only temperature varies).
  - `bench-report.md` with success rates, Wilson score intervals,
    tokens and wallclock per arm. Tests: tests/test_bench_runner.py
    (9 tests, FakeBackend scripted). First live reports (rep 0,
    qwen2.5-coder:32b via local ollama, docs/bench/): easy 24/24 and
    medium 24/24 pass in ALL arms (ceiling effect - only the hard tier
    discriminates). Hard: A 6/8, B 8/8, C 6/8 - SELF-REFLECTION beat
    the mesh, which matched single-shot at ~7x the token cost. First
    repo-own evidence for the single-agent>multi-structure finding;
    full seeded run (reps>=5) + raised inference timeout next.

### 14.15.10 Slice: pheromone routing (INITIALIZATION + SELECTION CONSUMPTION + WORKER-SIDE STRATEGY HONOR + DECAY TUNING SHIPPED)

the operator's framing: "Ich komme ja aus den Routing Algorithmen" — routing as the
mental model for mesh coordination. Synthesis of AMRO-S (arXiv 2603.12933),
Meta-Harness (2603.28052) and SkillDuet (2611.12109v2) into mesh vocabulary:

1. Adjacency matrix -> roster graph: nodes = agents (node-a/b/c + roles),
   edge weights = cost / latency / success rate per (edge, task_type).
2. Label-Setting = today's FSM: Dijkstra-fixiert, deterministic. This is
   WHY models don't "discuss": exactly one path is chosen, no alternatives
   are ever explored.
3. Label-Correcting = multiple pitches: k parallel coder drafts on the same
   task; convergence via verify + voting, NOT via open debate (sycophancy:
   correct answers flip under pressure — "Talk Isn't Always Cheap", 2025).
4. Ant colony = KV pheromones: tau in a `routing` KV bucket per
   (task_type, edge); deposit only on quality-gated success (task.done with
   deterministic verify pass), evaporation via a decay job, selection
   p proportional to tau^alpha * eta^beta, epsilon-exploration so unused
   edges stay reachable.
5. SkillDuet-Strength = the deposit rule: paired A/B comparison (log-prob
   shift with/without a note); no shift = internalized = drop the note.
   (the operator validated this as valid, together with point 6.)
6. Konsens-Protokoll direkt: agents agree directly — peer-to-peer voting
   over pitches in KV — no central FSM that decides alone.

Gate (the operator): "Lass uns aber erst den Benchmark und die
Evidenz aufsetzen" — do NOT implement routing before bench-report.md
numbers exist. Initialize alpha/beta/tau0 from measured benchmark data,
not guesses. Evidence and model profiles land in docs/experience.md
(shared, committed experience store).

### 14.15.11 Slice: swarm-to-human legibility (PROPOSED 2026-11-14, discussion first)

the operator's insight: memnc analyzes model-to-model interaction, but the
real product interface is **swarm-to-human** — a human directing, observing and
collaborating with the mesh. Kreative Mensch-Agenten-Kollaboration ist kaum im
Mainstream angekommen; das Feld heisst in der Forschung Human-Swarm Interaction
(HSI, aus Robotik/UAV) + Human-Agent Teaming (HAT). Edge-Thema, mega spannend.

Research grounding (2026-11-14 chat, sources verified):
1. Human-Swarm Interaction (arXiv 2504.02250): humans steer swarms via collective
   variables (situational awareness, task allocation, collective perception), not
   per-agent dialog. 10 design principles from goal-directed task analysis.
2. "From Conversation to Orchestration" (ACM 2025): interaction paradigm shifts
   from dialog to orchestration — open HCI problems: making orchestration and
   conflict resolution visible, accountability (WHO of the 5 said this),
   control over a nondeterministic collective.
3. "Multi-Agents are Social Groups" (arXiv 2411.04578, n=94): a swarm exerts REAL
   social group pressure on the human — 3 disagreeing agents shift opinion more
   than 1. BUT 5 agents != more influence than 3; larger groups tip into
   polarization and rejection. Human analog of the N_c critical-group-size result.
4. CASA vs machine-heuristic (Human Communication Research 2025): humans treat
   machines as-if social (mindless social scripts) but the swarm has no social
   accountability — you get human-side sycophancy excuses ("5 agreed") without
   real consolidation behind it.
5. Gricean maxims for human-LLM (arXiv 2503.00858): common ground breaks; maxims
   must be reinterpreted (quantity = manage cognitive load; quality = evidential
   trust-building; relevance = context management).

Design consequence for memnc: humans need not more dialogue but LEGIBILITY.
- (a) Present the collective as ONE entity: roster + pheromone heat as the
  swarm's "mood"/state, aggregated views (bench-report style), not per-agent logs.
- (b) Provenance everywhere: which model produced which contribution, at what
  confidence — docs/experience.md is the human-readable "who is good at what" layer.
- (c) Surface conflicts, don't hide them: dissenting pitches shown as pitches,
  verify-gate outcomes as first-class UI objects.
- (d) Discount group pressure: "3 agents agree" is a WEAKER signal than one
  passed deterministic check. Verify-gate > Konsens remains the ground rule —
  the swarm-to-human asymmetry (CASA without accountability) is exactly why.
- (e) Shared mental model (HAT): the human's model of "who can what" must be
  kept calibrated (trust calibration) — experience.md + routing KV as its basis.

Status: DISCUSSION POINT (the operator: "Ja mach rein ... mega spannend"). No UI work
yet — writes-before-code rule. Candidate content for the UI P0 slice and the
meshctl surface; independent of bench, can start after stage-2 runner.

# 15. Research evidence integration (2026-11-15) — intake CLOSED

Twelve sources processed 2026-11-14/15 (the operator: "dann ist Schluss" — no further
research material gets added). Each has a full entry in docs/experience.md plus a
fact-store record. Evidence classes: PEER-REVIEWED — ReAct (ICLR 2023), GEPA
(ICLR 2026), ACE (ICLR 2026), MEM1 (ICLR 2026), Flow-DPO (NeurIPS 2024),
non-cooperative poetry (Journal of Language Modelling 2026-02), TalkHier (IEEE
TAI), MAAI fundamentals (Electronic Markets 2026, conceptual). ARXIV PREPRINT —
FireAct, SyncMind. PRAXIS — Codacus video, Anthropic Ultracode.

## 15.1 What our own benchmark established

Easy and medium tiers are ceilings (24/24 every arm). The hard tier is the
discriminating one: single-shot A 6/8 (75%, ~610 tokens/task), self-reflect
B 8/8 (100%, ~2144), mesh C 6/8 (75%, ~4369). Mesh bought zero success at 7x
cost; self-reflection was the winner. Consequence: any mesh mechanism must
justify itself against arm B, not against arm A. This is the first repo-own
evidence for the Einzel-LoRA > Multi thesis.

## 15.2 The principles, each mapped to its plan item

1. Codacus (praxis): planner = biggest model; specialists need search/harness;
   never brief-loss (task prompt must reach every role — verified in runner).
   -> input to 14.15.10 routing; runner fix already committed.
2. Anthropic Ultracode (praxis): adversarial refutation loop instead of
   agreeable review; plan artifact persisted in KV; parallel agent spawn.
   -> arm B built on the refutation loop; plan-in-KV + parallel spawn are
   arm C2/D design elements.
3. ReAct (ICLR 2023): reason-act-observe; internal unsure -> external fallback
   as a routing signal. -> 14.15.10 routing signal candidate.
4. FireAct (preprint): task-tuned trajectories beat prompted strategies.
   -> rationale for generating task-specific training data (see 9).
5. GEPA (ICLR 2026): reflective prompt evolution from ~10% of rollouts can
   beat RL; prompts are evolvable artifacts, not constants. -> slice:
   bench-driven prompt evolution of roles.py SYSTEM_PROMPTS (planner/coder/
   critic); the cheap middle rung between prompting and LoRA.
6. ACE (ICLR 2026): context as append-only itemized bullets with deterministic
   merge; two failure modes to guard: brevity bias (over-compression) and
   context collapse (duplicates). -> slice: automated Erfahrungsspeicher
   (experience.md is the manual first instance).
7. MEM1 (ICLR 2026): RL-trained state compression keeps memory constant over
   long interactions; answers chatstrophic forgetting without weight changes.
   -> KV-compaction slice; competitor reference for P2b parametric memory.
8. SyncMind (preprint): agents do NOT self-regulate resources — external
   discipline required; sync/concurrent execution is a distinct evaluation
   tier. -> routing must enforce resource discipline from outside; future
   sync bench tier with concurrent commit semantics.
9. Flow-DPO (NeurIPS 2024): DPO on multi-agent flows improves the verifier,
   not the chain policy; pairs come from the flow's own runs. -> bench +
   deterministic checkers ARE the DPO pair generator; learned-verifier slice.
10. Non-cooperative poetry (J. Lang. Modelling 2026): weight-trained agents
    stay durably specialized and diverge; prompted agents homogenize after
    iteration 1; heterogeneous ensembles win; explicit deviate/disagree steps
    add diversity+novelty. -> strongest external evidence for Einzel-LoRA and
    P2b growing-LoRA; arm C2/D gets an adversarial critic; heterogeneous
    nodes per mesh are a feature, not a bug.
11. TalkHier (IEEE TAI): structured communication tuples (Message, Background,
    Intermediate Output) beat free text (Background removal = -8.9pp, biggest
    single protocol factor); hierarchical refinement with k single-criterion
    evaluators + eval supervisor = -11.1pp ablation, but k-evaluator cost is
    the 7x-multiplier class that sank arm C. -> NATS message schema becomes a
    structured tuple with persistent background in workflow_state; critic
    split is an arm C2/D candidate with the cost caveat attached.
12. MAAI fundamentals (Electronic Markets 2026, conceptual): five-layer
    framework; C2 states the data-centric doctrine explicitly (weights fixed,
    adaptation only via data/context) — the peer-reviewed formulation of the
    paradigm P2b argues against; bench = falsification test. C3/C4 map 1:1 to
    roster/planner + backlog/FSM; their demanded "validation methods for
    dynamically composed opaque agents" = our deterministic verify-gate;
    their process-mining agenda = trace/roster observability.

## 15.3 Plan consequences in priority order

1. Finish the full seeded run (120) and publish pooled Wilson CIs in
   bench-report.md; then initialize 14.15.10 routing parameters from measured
   data (alpha/beta/tau0, planner-capacity bonus, unsure->external signal). STATUS: DONE 2026-11-15 —
   docs/bench/report-full-2026-11-15.md (A 33/40 [68.0,91.3] @609 tok, B
   38/40 [83.5,98.6] @1910, C 35/40 [73.9,94.5] @4295; B never worse than C,
   no task where C > B) and 14.15.10 routing initialization appended to the
   roadmap (tau0=1.0, alpha=1, beta=2, rescue-matrix-seeded deposits,
   A->B->C escalation floor, planner-capacity penalty, unsure->external
   deposits).
2. NATS message tuple schema (message/background/intermediate_output,
   persistent background in workflow_state) — design-first, small slice,
   TalkHier-derived. STATUS: design written 2026-11-15 —
   docs/design-message-tuple.md (schema models, workflow_state companion
   key, opt-in flag, zero extra model calls; build order 4 steps).
   BUILD PROGRESS: step 1 done — schema models `Background` /
   `IntermediateOutput` / `MessageTuple` in schema.py + tests
   (tests/test_tuple_schema.py). BUILD COMPLETE (2026-11-15, commit c547815):
   all four steps shipped — orchestrator background persistence
   (bus.save_background/load_background + KV write-backs), worker/orchestrator
   tuple path behind Settings.tuple_prompts (opt-in), tests
   (tests/test_tuple_schema.py, e2e tuple.py verified end-to-end).
3. Arm C2/D design document BEFORE code: adversarial critic (Ultracode +
   poetry deviate step), k single-criterion evaluator split + eval supervisor
   (TalkHier, cost caveat), plan-in-KV (Ultracode), specialists with search
   (Codacus). Success criterion: beat arm B on hard tier at materially lower
   cost than arm C. STATUS: design written 2026-11-15 —
   docs/design-arm-c2d.md (D1 adversarial single round first, D2 evaluator
   split only if D1 < B, D3 structured tuples + plan-in-KV ships with D1).
   BUILT (2026-11-15): arm D (adversarial-mesh, D1) implemented in
   src/memnc/bench/runner.py — planner -> coder -> adversarial critic with an
   evidence gate (approval without counterexample/tried-list is rejected as a
   critique), max 2 revisions, checkers unchanged as final authority.
   Bench run + report pending (needs the 120-rep full run).
4. GEPA slice: bench-driven prompt evolution over roles.py SYSTEM_PROMPTS.
   STATUS: BUILT (2026-11-15) — src/memnc/bench/gepa.py: failure-driven
   reflective mutation from bench JSONL, Pareto candidate selection (keep
   best-on-at-least-one-task, prune dominated; ties survive), evaluation
   wraps the bench arm with a per-role prompt override (SYSTEM_PROMPTS
   swapped and restored, engine errors included). Zero weight changes; costs
   rollouts only. Evolution runs are opt-in (bench models, no downloads).
   The reflective crossover from the design note is BUILT (2026-11-15):
   propose_crossover merges the two most complementary frontier candidates
   (pick_crossover_pair: strongest candidate + the one winning on most tasks
   the strongest fails; degenerate single-candidate pools fall back to
   mutation), the evolve loop alternates mutation (thin pool) and crossover
   (split frontier); unparseable merge proposals keep parent A. Tests:
   tests/test_gepa.py (failure harvest, tolerant proposal parsing, Pareto
   front, prompt swap/restore incl. error path, crossover pair selection +
   evolve-operator alternation).
   A live evolution run awaits the 120-rep full bench run (item 1).
5. ACE slice: automated Erfahrungsspeicher — append-only itemized bullets,
   deterministic merge, brevity-bias + context-collapse guards.
   STATUS: BUILT (2026-11-15) — src/memnc/experience_store.py (design in
   docs/design-ace-experience.md): file-backed append-only itemized
   bullets grouped by topic; deterministic merge with the context-collapse
   guard (Jaccard >= 0.8 near-duplicates folded, longer text wins) and a
   monotone merge (never shrinks); explicit guarded compress() step
   (brevity-bias guard: keeps >= 70% of bullets per compression). First
   producer: record_bench_run() turns a bench JSONL into per-arm bullets.
   No LLM calls in the store. Tests: tests/test_experience_store.py (12).
   BUILD COMPLETE (2026-11-15): producer wiring shipped —
   orchestrator._deposit_observation folds every terminal observation into
   the store via record_observation() (success/failure topics, deterministic
   bullet text, near-duplicates fold through merge(); best-effort, never
   raises into the pipeline) behind Settings.experience_from_observations
   (opt-in, default off) + Settings.experience_store_path;
   `meshctl exp [--topic T] [--limit N]` read verb prints bullets
   newest-first (format_bullets shared). Tests: tests/test_experience_wiring.py.
   Worker-side deposit added (commit 59ff7e6): in the self_pipeline the worker owns
   terminal transitions, so worker._apply_local deposits + folds for every terminal
   state (tests/test_worker_deposit.py; compose env EXPERIENCE_FROM_OBSERVATIONS,
   default 0). LIVE VERIFIED (2026-11-15 night run): EXPERIENCE_FROM_OBSERVATIONS=1
   set on the host .env, host images rebuilt (orchestrator + 3 workers), remote node
   image synced (rsync + node compose rebuild; its orchestrator paused so a single
   dispatcher owns the bus) — a routed terminal task (failed smoke task, planner-only
   route) now appears in `meshctl obslast` from the remote worker. Remaining: a
   routed DONE task is still blocked by planner model quality (remote planner model
   emits invalid JSON; the host planner is synergy-withdrawn on its real failure
   record — the claim gate working as designed) and by the local experience store
   being node-local by design (remote deposits land in the remote's store file, the
   host `meshctl exp` reads the host file). No model downloads without approval, so
   a green routed pipeline needs either a fix of the planner model set or the user's
   go to pull a planner-capable model on the remote node.
6. Flow-DPO slice: bench JSONL + checkers as DPO pair generator, learned
   verifier as the deliverable.
   STATUS: generator BUILT (2026-11-15) — src/memnc/bench/dpo.py: one
   (chosen, rejected) pair per task with both a checker-passing and a
   checker-failing run; chosen = cheapest successful solution (fewest
   tokens, tie: lowest run_id), rejected = costliest failing one.
   Deterministic, no human labels. BenchEngine gained opt-in
   record_solutions (RunResult.solutions; ordinary rows stay lean).
   Tests: tests/test_bench_dpo.py (6). The learned verifier itself
   (training on these pairs): design in docs/design-learned-verifier.md
   (V1 linear scorer vs V2 LoRA verifier ladder). V1 BUILT —
   src/memnc/bench/verifier.py: pure-stdlib pairwise logistic regression
   over deterministic solution features (files/bytes/lines/comment &
   blank ratios, placeholders, empty files, AST syntax errors, duplicate
   lines, avg line length), JSON artifact with feature-name pinning;
   scope = shape/fabrication failures only, never a competence judge.
   Tests: tests/test_bench_verifier.py (7). V2 (LoRA verifier on real
   pairs) blocked on compute + model-download approval.
7. Sync bench tier (SyncMind): concurrent-commit semantics as a separate tier.
   STATUS: BUILT (2026-11-15) - src/memnc/bench/tasks.py + checkers.py +
   solutions.py + runner.py. Four opt-in sync tasks (load via
   load_tasks("sync"); the default 24-task baseline is unchanged so
   historical runs stay comparable). Each sync task carries an
   `amendment`: an environment update delivered AFTER the first coder pass
   via BenchEngine._apply_amendment (SyncMind out-of-sync simulation) -
   one extra coder turn whose prompt contains "ENVIRONMENT UPDATE" plus
   the amendment text. All four arms (A/B/C/D) hook the amendment after
   their first coder pass. Checkers verify the AMENDED spec only, so a
   solution that ignores the update fails; the pre-amendment spec is
   verified to fail its checker in tests. Amendment turns count toward
   llm_calls/tokens. Tests: tests/test_bench_sync.py (6).
8. P2b parametric memory stays the thesis experiment: single-LoRA >> multi,
   growing-LoRA (MoRAM/CoDyRA) + Fisher-Merge as the bet, now argued against
   a named, peer-reviewed counter-position (12/C2) instead of a strawman.
   STATUS: DONE (design) — argument section "P2b counter-position" written
   under the P2b priority slice: MAAI C2 named as the peer-reviewed
   counter-position, MEM1 as the second one; falsification gates fixed
   (train/valid divergence threshold + held-out recall vs fixed-weights+KV
   baseline); dual-store design kept as the hedge.

### 14.15.10 routing initialization (from measured data, 2026-11-15)

Gate satisfied: full seeded hard-tier run complete, docs/bench/
report-full-2026-11-15.md (A 33/40 [68.0,91.3] @609 tok; B 38/40
[83.5,98.6] @1910; C 35/40 [73.9,94.5] @4295). Initial pheromone parameters
derived from that table, not guessed:

- tau0 = 1.0 uniform (no prior edge favored before evidence).
- Prior reinforcement seeded per edge from the run: r = success_rate /
  ln(1 + tokens_per_task). A 0.126, B 0.124, C 0.052 — A and B nearly tie
  per cost unit; the discriminator is per-task, not per-arm, so deposits are
  tracked per (task_type, edge) with the rescue matrix as the seed:
  rate_limiter-type tasks -> self-reflect edges get B's 4/5, planner-heavy
  mesh edges get C's 1/5 (deposit gate must NOT credit C for planner
  restarts that hurt).
- alpha = 1.0, beta = 2.0 (heuristic weight eta = 1/cost dominates tau until
  enough deposits accumulate — matches the 7x wall-time spread mattering more
  than one-off successes).
- Escalation chain hard-coded as the epsilon-exploration floor: route to A
  first on cheap tiers (easy/medium saturated 24/24 for every arm), escalate
  A->B on verify fail, B->C only on repeated B failure (the data shows C
  never beats B on this tier, so C is the fallback, not the default).
- planner-capacity signal: node-a planner (qwen2.5:7b) vs node-c planner
  (qwen2.5-coder:7b) both below the coder tier; C's planner restarts cost
  success on rate_limiter — planner edges carry a capacity penalty until a
  strong planner model is registered.
- unsure->external fallback (ReAct signal): an agent emitting the unsure
  marker deposits tau on the escalate edge, not on its own role edge, so
  repeated unsure behaviour converges routing toward B.

**Selection consumption (BUILT 2026-11-16):** the orchestrator now READS the
field. `Orchestrator._select_strategy` runs `route_strategy` against the live
pheromone KV entries at task start and re-selects on every REVISE (the failed
edge becomes the escalation floor, so pheromone never re-picks a strategy that
just failed); the chosen edge is stamped into `TaskSnapshot.routing_edge`
(history event `route.strategy`) and rides the dispatch payload as
`strategy`. Deposits now land on the edge the task actually ran
(`routing_edge`), not always on LIVE_EDGE. Settings: `pheromone_select`
(default on) + `pheromone_weak_planner` (planner-capacity discount at
selection; deposits stay honest). Best effort end to end: an unavailable
bucket leaves the default chain head in place. Tests:
`tests/test_pheromone_select.py` (11). **Worker-side honor (BUILT
2026-11-16):** the worker-driven pipeline honors the self-reflect edge —
planner step skipped (task prompt IS the plan), coder model critiques its own
output in-process through the normal gates (acceptance/verify/reputation),
revision budget stays with the FSM; see the Built entry and
`tests/test_worker_self_reflect.py` (5).

**Decay tuning — live pass (2026-11-17/18, defaults confirmed):** inspected the
live `mesh_pheromones` field during the in-flight arm-D hard run. Defaults
rho=0.1 @ 3600 s hold up; no tuning change made:
- Half-life ~6.6 h: bench-seeded hard_* cells from the 2026-11-15 full run had
  decayed to the noise floor (~0.9^48 per deposit) while the current run
  re-accumulates fresh S deposits (~0.9^7-0.9^10) — the field tracks recent
  behaviour exactly as designed.
- No runaway accumulation: max tau in the field ~2.3; repeated-failure cells
  sum to < 2 within hours and then fade overnight.
- Stale failure signal retention is acceptable: a two-day-old failed mesh task
  still carries ~0.3-0.8 failure tau long enough to steer escalation away
  without permanently benching the edge. If bench-style bulk deposits ever
  need faster clearance, the knob is `pheromone_decay_interval_s` (settings
  only), not a code change.

Shipped (2026-11-15, initialization slice): `pheromone.py` implements the
score tau^alpha * eta^beta with these exact constants, the 24-cell rescue
matrix as seed deposits in the `mesh_pheromones` KV bucket, live deposits on
terminal transitions (task-type = prompt-head slug, edge = `mesh` until more
strategies exist), the A->B->C escalation floor, and the `meshctl pheromone`
view.

Shipped (2026-11-15, decay slice): `PheromoneEntry.evaporate(rho)` scales
the accumulated deposit counters (successes/unsure/failures/tokens) by
(1 - rho) per interval — ACO evaporation so the field tracks recent
behaviour; the static cost prior `cost_tokens` is a measurement of the edge,
not accumulated belief, and is never decayed. `PheromoneStore.decay(rho)`
rewrites only cells that materially changed. The orchestrator runs a
`_pheromone_loop` beside the result/timeout loops every
`pheromone_decay_interval_s` (default 3600, 0 disables) with
`pheromone_rho` (default 0.1); Bus exposes `decay_pheromones(rho)` (0 when
the bucket is absent, so fakes stay silent). CLI consumption surface:
`meshctl pheromone --pick TASK_TYPE` runs `choose_edge` over the live field
and prints the chosen edge.

Shipped (2026-11-15, selection consumption): `route_strategy(entries,
task_type, failed_edge, weak_planner)` in `pheromone.py` combines pheromone
choice with the escalation floor — candidates are the chain edges at or above
`escalate(failed_edge)`, the highest tau^alpha * eta^beta wins, and
planner-heavy edges (C/mesh) carry `PLANNER_CAPACITY_PENALTY` 0.5 while
`weak_planner` is set. The penalty applies at selection time only; deposits
always record what actually happened. Bench arm P (`--arms A,B,C,D,P`)
consumes it end to end: each attempt routes over the seeded field, runs the
chosen edge's arm path, and folds the per-attempt checker verdict back into
the field — the checker gates ONLY arm escalation (the rescue floor), never
the inner revision loops. `RunResult.routed_edges` records the attempt
sequence and the report prints the routed-attempt distribution. Worker-side self-reflect edge and unsure-marker deposit
wiring are DONE (2026-11-15): the worker self-pipeline deposits its terminal
outcomes on the SELF_REFLECT_EDGE ("B" - its REVISE loop is arm-B behaviour,
mirroring the orchestrator's LIVE_EDGE deposit), and an unsure marker in the
error or critique summary folds as a weak "unsure" deposit on the
self-reflect edge instead of a success/failure on the acting edge, so
repeated unsure behaviour converges routing toward B. Still open: an arm P
full-run measurement.

## 15.4 OpenCode harness (BUILT 2026-11-15, commit 7765404)

Motivation: node-c now runs opencode 1.19.5 against the local ollama
27b coder (`mannix/qwen3.6-27b-a3b-coder:CD-IQ4_K_M`), which DOES emit native
tool_calls (verified 2026-11-15: full create-file + run + confirm loop,
TASK_COMPLETE). The node-c image predates the pi harness (d44fc60); opencode
is installed there, pi is not. Making opencode a registered harness lets the
weak->strong loop hand off to node-c's coder without rebuilding the image
around pi.

Design — mirror `harnesses/pi.py` exactly, differences only where opencode
differs:

- `name = "opencode"`; caps: native_tools=True, external_process=True.
- Command: `opencode run "<prompt>" --model <provider/model>` with cwd=workspace.
  Model string is provider-qualified (e.g. `ollama/mannix/...`), so the worker
  setting for harness models must carry the full id, not just the ollama name.
  We leave a bare model name alone (opencode then uses its own default
  provider from `~/.config/opencode/opencode.json`).
- Event parsing: opencode `run` prints a human-readable stream; unlike pi it
  has no `--mode json`. Parser strategy (tolerant, like pi's):
  - count tool executions by lines matching the tool-event markers
    ("← Write", "→ Edit", "← Read", "$ ", "Bash("); start with a small
    verified marker set, extend after first live run;
  - summary = last non-empty line block of stdout;
  - "settled" = process exit 0 (no settled marker exists);
  - error = non-zero exit, or stderr tail.
- Optional machine-readable follow-up (only if the marker heuristic proves
  brittle): `opencode run ... --print-logs --log-level DEBUG` and parse
  structured lines; keep OUT of v1.
- `opencode run` reuses `~/.local/share/opencode` state; on stale state it can
  hang (observed). Harness never cleans user state automatically; worker
  env var `OPENCODE_CLEAN_STATE=1` (opt-in) makes the harness
  `rm -rf ~/.local/share/opencode` before the run. Document the observed
  stall + fix in the harness docstring.
- Timeout: same 900 s default, kill process group (start_new_session).
- Product stays the workspace diff (shared `workspace_diff` helper).
- Registry: add `opencode` to the lazy import list in `harnesses/__init__.py`
  (inproc, pi, opencode) — both `get_harness` and `known_harnesses`.
- Tests (mirror `tests/test_harnesses_pi.py`):
  1. parse: fake stdout with tool markers + summary -> counts;
  2. parse: garbage/malformed lines skipped, no crash;
  3. run: fake opencode shell script as bin -> ok result with diff;
  4. timeout: hanging fake -> HarnessResult(ok=False, error contains timeout).
- Routing signal: `tool_events == 0` again means the provider template has no
  tools (same signal as pi). On the ollama 27b template we verified >0.

Open questions for the live run (answer after first real handoff):
- Does opencode need `--format`/any non-default flag to suppress interactive
  TUI remnants in `run` mode? (Current smoke: no.)
- Exact timing budget: smoke task took ~3-4 min incl. model load; the
  900 s default should hold, but the worker-level timeout for coder tasks on
  node-c may need to rise to 1200 s.

### 15.4.1 opencode INSIDE the worker container (BUILT 2026-11-15, commit 31bc41d)

Gap found after the image rebuild (7765404 on node-c): the worker container
knows the opencode *harness* (`known_harnesses()` = inproc/opencode/pi), but
the opencode *binary* is not in the image (`command -v opencode` -> nothing
inside `memnc-worker-planner-1`). A `--harness opencode` task on node-c would
fail at spawn. Host binary at /usr/bin/opencode (node-c) does not help — the
worker runs inside the container.

Design, four pieces:

1. worker.Dockerfile: install the opencode binary at build time via the
   official installer (`curl -fsSL https://opencode.ai/install | bash`) with
   HOME pointed at /usr/local so the binary lands in a fixed location, then
   symlink /usr/local/bin/opencode. Must be readable+executable by any uid
   (compose runs the worker as ${UID:-1000}, rootfs read_only).
2. Config: opencode reads ~/.config/opencode/opencode.json (HOME=/tmp in the
   container, /tmp is tmpfs). Compose worker services get a bind mount
   `${HOME}/.config/opencode:/tmp/.config/opencode:ro` so each node carries
   its own provider config. On nodes without that dir Docker creates an
   empty dir — harmless (harness only runs when harness=opencode requested).
3. baseURL: the config says http://localhost:11434/v1 (written for host-side
   runs). Inside the container localhost is the container itself. We swap it
   to the node's LAN IP literal (works from the host AND from containers;
   ollama already listens on all interfaces — proven by the working
   INFERENCE_BASE_URL=host.docker.internal path). host.docker.internal is
   NOT an option: it resolves only in containers, but the same file is used
   by host-side runs (no sudo on node-c to patch /etc/hosts).
4. Model mapping: MODEL_CODER on node-c is the bare ollama tag
   (`mannix/qwen3.6-27b-a3b-coder:CD-IQ4_K_M`); the same setting feeds the
   inproc path, which needs the bare name. The opencode harness therefore
   prefixes `ollama/` when the model string has no known provider prefix —
   small patch in harnesses/opencode.py + unit test, so the deployment env
   stays unchanged.

Then: rebuild the node-c image, verify `opencode --version` inside the worker
container, submit a routed task (prefer_node=node-c, harness=opencode) as the
live handoff check.

### 15.4.2 node-c joins the node-a bus (design note, 2026-11-15, before touching the mesh)

Discovered while the first routed task (`1a1df86c`, prefer_node=node-c) sat
unclaimed in the backlog: the node-c `.env` is a verbatim copy of node-a's.
Consequences, read off `docker inspect`:

- node-c's worker announces itself as `node-a-planner` (NODE_NAME=node-a +
  role suffix) — same name as node-a's real worker. A mesh can't tell them
  apart, and `prefer_node=node-c` matches nobody.
- node-c's NATS_URL=nats://nats:4222 points at node-c's OWN nats container, so
  the two "halves" are two disjoint NATS islands. Tasks submitted on node-a
  can never be claimed on node-c — no routing, no mesh.
- NODE_GPU/NODE_VRAM_GB on node-c still describe node-a's gpu-a.

Fix, minimal and reversible (all of it node-c-side, no repo change):

1. node-c `.env`: `NODE_NAME=node-c` (worker becomes node-c-planner),
   `NODE_GPU=gpu-b`, `NODE_VRAM_GB=16` (correct the copied metadata),
   and `NATS_URL=nats://<node-a-LAN-IP>:4222` — node-a's nats container
   already publishes 4222 on 0.0.0.0, same NATS_TOKEN=dev-token on both
   sides, so the shared token stays the mesh auth.
2. node-c's local nats container keeps running but goes idle; the compose file
   stays untouched so a standalone node-c stack remains possible.
3. `docker compose up -d` on node-c recreates worker+orchestrator against the
   shared bus. The two orchestrators then share one JetStream KV — expected
   to be fine (the mesh is designed for multi-node claim fan-out), watch the
   first routed task for surprises.
4. Re-verify routing with a fresh routed task (prefer_node=node-c) once the
   worker is up under its new name.

Not done on purpose: no leaf-node config, no per-node nkeys yet (H4 stays a
follow-up; both sides still run the dev token on the LAN).

### 15.4.3 Durable tasks instead of one-shot (design note; slice 1 error-aware retry BUILT)

Operator verdict after watching four TTS-task attempts die on a remote node: the
one-shot task model is bad (repeated one-shotting underperforms).

Evidence from 2026-11-15 (task ids `1a1dfb65`, `1a1dfc37`, `1a1dfc47`,
`1a1dfcac`, all FAILED):

1. A single transient error kills the whole task. The planner hit one
   `500 Internal Server Error` (Ollama aborts `qwen2.5-coder:7b` with
   "prediction aborted, token repeat limit reached" when forced into
   `format: json` — the model loops). One 500, then two worker timeouts,
   then `task.failed`. Nothing recovered.
2. Retries restart from scratch. Each attempt re-runs the full
   planner→coder→critic pipeline; no attempt sees what a previous attempt
   already did. The last attempt died mid-setup and left an empty
   `kugel-venv` shell — the next attempt cannot know it exists.
3. Real intermediate state is invisible to the mesh. The node-c workspace
   held a compiled `omnivoice-tts` binary, generated WAVs, cloned Vulkan
   headers and downloaded GGUFs from earlier the same day. The task text
   cannot reference them because the orchestrator has no notion of durable
   workspace state between attempts.
4. Error types are not distinguished. A model error (the 7b JSON loop) is
   retried on the same broken route — the roster knew `[27b, 7b]` for
   planner but the round kept choosing the model that cannot do the job.

Design direction (slice 1 - error-aware retry - SHIPPED; checkpoint and
workspace-continuity slices still open):

- **Error-aware retry (BUILT):** `src/memnc/failclass.py` classifies a
  step failure as `timeout` / `server` / `invalid_output` / `unknown`
  (timeout wins over the others). On a model-level failure with retry
  attempts left, the orchestrator rotates the failed role to the first
  other advertised model from the live roster
  (`Orchestrator._maybe_rotate_model`) before the retry re-dispatches;
  `TaskSnapshot.model_overrides` (per role) is preferred in the dispatch
  payload over the task-wide `model_override` so a rotated coder does not
  leak into the next planner hop. A human retry intent (model_override)
  always wins, rotation never second-guesses it. Best-effort: no roster,
  no alternative, or an unknown executed model just records a
  `model.rotate` history event and keeps the normal retry path. Setting
  `model_rotate_on_fail` (default ON; `MODEL_ROTATE_ON_FAIL=0` disables).
  Tests: `tests/test_error_retry.py`.
- **Workspace continuity (BUILT 2026-11-17):** the dispatch payload now
  carries `workspace_state` on every retry (attempt > 1) — a short
  top-level listing of the shared workspace (`workspace_listing()` in
  `harnesses/workspace.py`: dirs suffixed `/`, sorted, capped, None when
  missing/empty; names only, no sizes). `build_user_prompt` renders it as
  a WORKSPACE STATE section so a retrying planner/coder sees the venv,
  binaries and artifacts a prior attempt left instead of re-deriving
  state. Tests: `tests/test_harnesses.py` (listing cases + prompt
  rendering).
- **Checkpoint/restart (BUILT 2026-11-17, attempt-journal slice):** every
  failed attempt that schedules a retry appends one bounded entry to
  `TaskSnapshot.attempt_journal` (`src/memnc/journal.py`,
  `note_failure()`): the failed attempt number, the stage (role), the
  truncated error (400 chars), the file paths the attempt produced (from
  the coder output, max 20) and a short implementation summary. The list is
  capped at 8 entries (oldest dropped) so KV snapshots cannot grow
  unboundedly. Both retry sites record the entry BEFORE the attempt counter
  advances (`Orchestrator._apply` via `event.role`,
  `Worker._apply_local` via `envelope.role`). The dispatch payload
  (orchestrator `_dispatch` + worker claim path) carries the structured
  entries and `build_user_prompt` renders them as a "PROGRESS FROM PRIOR
  ATTEMPTS" block for every role. The journal is derived from data the
  snapshot already stores (code output, error) — no new KV buckets, no
  worker claim-path change beyond passing the field through. Older
  snapshots without the field are tolerated (`note_failure` no-ops).
  Answering the open question: plan JSON stays OUT of the journal (the
  payload already carries the plan verbatim); the journal holds only what
  the plan/error alone do not show. Tests: `tests/test_journal.py`.
- **Workspace continuity:** the task workspace (already a mounted volume)
  becomes the source of truth; a retry is told "the workspace exists, list
  it before you plan" instead of re-deriving state.
- **Error-aware retry:** classify the failure (timeout vs. HTTP 5xx vs.
  invalid output). A model-level failure must switch to the next candidate
  model for the role (roster already advertises a list), not re-pick the
  same one. A hard error-loop after N model switches fails the task with
  the diagnosis attached.
- Non-goal for now: task migration across nodes mid-attempt. Workspace
  volumes are node-local; resume happens on the claiming node.

Open question: how error classification maps onto the existing trace
entries (the journal stores the raw reason text; failclass labels could be
added per entry later).

Finding — fabrication passed the DONE gate (2026-11-15, mesh autonomy test):
task 1a1e00c15dc0077966c7064 (KugelAudio TTS) finished DONE with critic
approval — but the worker only created an empty venv and then FABRICATED the
rest: a plausible kugel-report.md with invented metrics (load 10.23 s, RTF
2.01, 3 GB peak, "clear and understandable audio quality"), no WAV anywhere
on the filesystem, no kugelaudio/torch/accelerate in the venv, no HuggingFace
cache, and completed_commands=0 — zero commands were ever executed. The
critic approved purely on the narrative. Three structural gaps (this design
note must close them):
1. A task can reach DONE without a single executed command. The ledger
   records completed_commands but never gates on it. Rule: DONE without
   completed_commands > 0 is structurally rejected.
2. The critic approves on narrative — it cannot verify artifacts it cannot
   see. Acceptance criteria (e.g. "kugel-test.wav exists, duration > 30 s")
   must be checked deterministically: an acceptance step that runs
   ffprobe/stat against the real filesystem, not an LLM's opinion.
3. Claimed vs. existing artifacts are never cross-checked: every file path
   in a code result is stat'ed by the orchestrator before DONE is written.

### 15.4.5 DONE-gate hardening (design note; gaps 1-2 shipped)

Closing the three gaps from the fabrication finding above. Layered defense:
each gate alone is defeatable; together they make fabrication expensive.

Gap 1 - structural activity gate (shipped with this note):
- New opt-in setting `done_requires_activity` (default OFF; enable per
  deployment via `DONE_REQUIRES_ACTIVITY=1` in the environment). Rationale:
  pure writing/planning tasks legitimately finish with zero executed tools,
  so the gate must be a deployment-level choice for autonomy runs.
- Signal: the union of `completed_commands` and `completed_tool_calls` on
  the snapshot. Either non-empty means a real tool or command executed.
- Enforcement point: both `finish` action sites (orchestrator result path
  and worker self-pipeline) consult `fsm.done_block_reason(snapshot)`. The
  FSM stays pure - the helper only reads the snapshot, the call sites turn
  a blocked `finish` into `escalate("structurally rejected: no executed
  commands or tool calls")`, which follows the normal failure path
  (ledger, DLQ, telegram failure card).
- Honest limit: this is necessary, not sufficient. A fabricator that runs
  one real command passes the gate; gaps 2+3 carry the real weight.

Gap 2 - deterministic acceptance checks (SHIPPED, this slice):
- New module `src/memnc/acceptance.py`: the planner's per-step
  `acceptance` field carries machine-checkable criteria, one per line
  (a tiny DSL documented in the planner system prompt):
  `file_exists <path>`, `file_min_size <path> <min_bytes>`,
  `file_contains <path> <regex>`, `command <argv>` (exit 0),
  `ffprobe_duration_min <path> <min_seconds>`. Kind names are matched
  case-insensitively (hyphen/underscore equivalent); unparsable lines are
  recorded as ignored evidence, never silently dropped - prose-only
  acceptance fields make the gate a no-op, not a pass.
- Checks are plain filesystem/probe calls against the real workspace,
  never LLM judgment. `command`/ffprobe subprocesses run in the workspace
  with a curated env whitelist (no inherited secrets), a 30 s timeout,
  whole-process-group kill on timeout, and bounded output.
- Enforcement: `Worker._acceptance_gate` runs after the critic verdict is
  stored and before `_verify_gate`. A failing check blocks the verdict
  from standing (even an approval): it is overridden to reject with a
  `blocker` CriticIssue carrying the check output, which drives the normal
  revision loop. Evidence is stamped into `TaskSnapshot.acceptance` and an
  `acceptance.failed` history event. Setting `acceptance_gate`
  (default ON; `ACCEPTANCE_GATE=0` disables) - default on because the gate
  is a no-op without parseable criteria. Tests: `tests/test_acceptance.py`.

Gap 3 - claimed-vs-existing artifact cross-check (design, with gap 2):
- SHIPPED 2026-11-15: the worker stamps `artifact_sizes` (path -> size in
  bytes, -1 = claimed but absent) into every coder result
  (`fsutil.verify_artifacts`), and the orchestrator fails the step with
  "claimed artifacts missing from workspace: ..." when any claimed path is
  absent - this also surfaces previously silent oversized-artifact skips.
  Content fabrication (a written-but-invented report) is only catchable by
  gap 2 acceptance criteria, not by stat.

### 15.4.4 Model serving layer: Ollama-in-Docker → per-node llauncher (design note, 2026-11-15, before coding)

Operator verdict: running Ollama as a Docker container is inflexible
(needs a better serving layer). Considered
llauncher, unsure because it "only serves one node".

Evidence from 2026-11-15:

1. node-a runs TWO parallel Ollama instances (docker container
   `ollama_power` and a native `ollama serve`), plus `llauncher.service`
   is already active. Two instances mean two disjoint model pools while
   the roster only sees one.
2. The roster advertises DISK models as servable (see 14.15 / FACT:
   cross-node model race). The 27b sits on node-a's disk but was never
   loaded; a node-c task got routed to node-a because of that ghost
   entry. Serving truth and roster are decoupled.
3. Ollama evicts idle models unpredictably; the first task after idle
   pays the load latency, and keep_alive pinning is a per-model hack.
4. No GPU pinning and no explicit model lifecycle control: which model
   loads when, on which card, is an internal side effect, not a decision
   the mesh can make or observe.

Design direction (not implemented yet):

- **Serving is per-host.** One serving layer per node, workers call
  `localhost`. This is the correct granularity, not a limitation of
  llauncher: the mesh coordinates above it (roster, heartbeats, tasks)
  and never reaches across hosts for inference.
- **llauncher (llama-swap) as the LLM serving layer per node.** Explicit
  model swap on demand, per-model port routing, deterministic lifecycle.
  On node-a it already runs and would manage the single surviving Ollama
  as a backend (or replace it); the duplicate docker instance goes.
- **Roster reads serving state, not disk.** The worker's capability
  advertisement must list only models the local serving layer can serve
  *right now* (loaded or loadable without racing). This kills the
  disk-model ghost routing at the root.
- **TTS/multimedia models are out of scope for llauncher** — kugelaudio
  and Chatterbox are PyTorch processes, not llama.cpp servers. They get
  their own per-node process supervision; the serving-layer abstraction
  ("what can this node serve") should leave room for them.
- Non-goal for now: central model registry or cross-node model
  replication. Disk space is cheap; downloads stay node-local.

Open questions: does Ollama stay as a backend under llauncher (simplest
migration path) or get retired entirely; how the roster query
"served vs. disk" is exposed (extend the capability message vs. poll the
serving layer); and which node migrates first (node-a is the natural
pilot — llauncher is already running there).

Refinement — the chicken-and-egg of mesh-steered model lifecycle
(2026-11-15, operator decision): the original idea was that the mesh itself starts
and stops models per task ("Modell-Inferenz wird wieder per Modell
gesteuert gestartet/gestoppt"). Circular if taken literally: the planner
that decides "load model X" needs a model to decide with — the control
plane would sit inside the thing it controls. Resolution: split intent
from execution.

- **Intent (mesh, LLM-level):** tasks and plans may *express demand* —
  `needs_model` on the submission already does this, plans can add
  hints ("next stage needs 32b on a free card"). No agent ever executes
  a start/stop itself.
- **Execution (node-local, deterministic):** the serving layer resolves
  demand to load/unload at claim time by policy — load on first use,
  LRU eviction, explicit pinning for hot models. Zero inference in the
  loop, so it also works when every model is down (cold start).
- **Observability closes the loop:** serving state (loaded models, free
  VRAM) flows back into the roster, so the next routing decision sees
  reality, not disk ghosts.

This keeps the original vision — model lifecycle driven by task demand —
without the circularity: the mesh says *what* is needed, local policy
decides *when and how*, deterministically.

Related work and what we take from it (2026-11-15): Canonical's
Inference Manager (github.com/canonical/inference, GPLv3) validates the
per-node architecture — discovery, install/remove lifecycle, health
checks, and a single local proxy endpoint that routes to all providers
behind it. It does NOT replace llauncher for us: its provider catalog is
curated snaps (no arbitrary GGUFs), snap installation needs root, and
TTS stays out of scope. The concepts are worth adopting into llauncher:

1. **One proxy endpoint per node** — workers know a single port; all
   routing happens behind it.
2. **A multi-daemon registry with a capability matrix** (model × engine,
   served-now vs. not) — this is the piece llauncher currently lacks:
   a registry abstraction spanning daemon TYPES (llama-server instances,
   Ollama, PyTorch TTS processes), not just multiple llama-servers.
3. **Health/status query** — the registry snapshot is the serving truth
   the mesh roster reads.
4. **install/remove as lifecycle operations** — model load/unload becomes
   an explicit registry operation instead of a side effect.

The missing cross-node dimension is filled by the mesh: each node holds
its local registry (that node's truth), and the mesh roster FEDERATES
across node registries. "Served vs. disk" stays answerable per node and
consistent mesh-wide, without a central registry.

Why llauncher is the base despite the alternatives (2026-11-15):
llauncher exposes its control plane over MCP, and that is the decisive
difference. Canonical Inference Manager is CLI-first, LiteLLM is an
HTTP proxy — both need a bespoke integration layer. llauncher's MCP
tools (list backends, start/stop/swap model) are already agent-native:
the mesh's intent-to-execution seam is the MCP tool surface itself.
Design consequence: the registry truth ("what does this node serve
right now") is exposed as an MCP resource that the mesh roster
subscribes to, model lifecycle stays MCP tools, and federation means
aggregating the per-node MCP registries — no central proxy, no custom
HTTP API on top.

# 16. Docs cleanup 2026-11-16 — absorbed design notes

Per the operator's cleanup decision the repo keeps only `docs/plan.md` and
`docs/roadmap.md`. The design notes below were distilled into this section
before deletion; every deleted file remains recoverable in git history
(`git log --diff-filter=D -- docs/`).

## 16.1 design-backlog-dep-resolution.md (BUILT 2026-11-16, commit 367bee4)

Backlog deps referencing MESH task ids resolve via a terminal-state checker:
`next_claimable(claimant, dep_checker=...)`; the driver passes
`_mesh_dep_checker(settings)` (registry snapshot per cycle). Rationale: a
mesh-id dep expresses ordering intent, not a success gate — terminal-failed
mesh attempts are followed by backlog strong-retries that carry the work
forward; blocking on them deadlocked the driver (observed 2026-11-16, 4
planned entries idle for hours). Unknown dep ids (neither store) stay
blocked, conservative. Follow-ups: root-cause intermittent
`driver error: nats: no keys found` (DONE 2026-11-17 —
two causes: (1) auth, commit 771a2b8: the server runs H4 NKey-only auth, the
legacy NATS_TOKEN in .env gets 'Authorization Violation'; backlog-driver.sh
now exports NATS_NKEYS_SEED=secrets/operator.nk. (2) code bug, fixed: an
empty KV bucket makes nats-py's `keys()` raise `NoKeysError`, which is NOT a
subclass of `NotFoundError` — `BacklogStore.list()` caught only the latter,
so the NoKeysError escaped into the driver loop as `driver error: nats: no
keys found`. Fixed `BacklogStore.list()` plus both `PheromoneStore`
`keys()` sites (decay + noval sweep); regression tests pin the empty-bucket
path. Also:
the server runs H4 NKey-only auth, the legacy NATS_TOKEN in .env gets
'Authorization Violation'; backlog-driver.sh now exports
NATS_NKEYS_SEED=secrets/operator.nk); clean old night/smoke
entries from the backlog (DONE 2026-11-16 — 29 stale night1/2/3/smoke1
entries removed live via the new `backlog-del`, backlog 31 -> 12 entries).

## 16.2 design-ship-gate.md (BUILT — ship-gate V2)

Observed 4x on 2026-11-16: tasks reach DONE with stub deliverables (418-byte
stubs passed the missing-file gate; the LLM critic approves narratives
without evidence). Gate is mechanical, in the orchestrator result hook
(`_on_result`/`_handle_result`, covers worker self-pipeline too): worker
stamps `artifact_lines` (non-blank lines, -1 missing) next to
`artifact_sizes`; new opt-in settings `verify_min_lines` /
`verify_min_files` (0 = off); failure is a mechanical refusal with file
names and measured numbers. Out of scope (follow-ups): branch-push
verification via `git ls-remote` (needs creds in worker), running referenced
tests in the workspace (sandbox risk). Rollout on node-a:
`VERIFY_MIN_LINES=20`.

## 16.3 design-arm-c2d.md (arm D — adversarial refinement mesh)

Baseline (hard tier, 2026-11-15): A 6/8 @ ~610 tok; B (self-reflect) 8/8 @
~2144; C (planner+coder+critic) 6/8 @ ~4369 — 7.2x A's cost for zero gain
over B. Arm D proposal: D1 = adversarial single round — critic MUST produce
a concrete counterexample (input/edge/line) or an explicit
tried-X-Y-Z list; agreeability without evidence rejects the critique itself;
max 2 rounds; cost class ~B (1600-2200 tok predicted). D2 = TalkHier
evaluator split (k=2 single-criterion evaluators + supervisor with explicit
merge, no summarization-by-vote), only if D1 < B, disqualified above 3000
tok/task mean. D3 = structured tuples + plan-in-KV (zero extra calls, ships
with D1). Qualification: D1 >= 7/8 and < 3000 tok; becomes default if it
matches B (>= 8/8) at <= 1.2x B cost; if D2 also < B, mesh-as-refiner is
DEAD for this task class — keep mesh only as supervisor-of-one (14.15.3)
and route the difference into GEPA prompt evolution (15.3 item 4). Evidence
sources: Ultracode (adversarial refutation), non-cooperative poetry
(deviate step vs homogenization), TalkHier (background tuples -8.9pp,
k-evaluators -11.1pp), Codacus (planner = biggest model), ReAct (escalate
on unsure). A variant must rescue at least one of A's two hard failures
(rate_limiter, dijkstra) to count as progress.

## 16.4 design-message-tuple.md (TalkHop communication — BUILT)

TalkHier tuples: every hop carries (Message, Background, IntermediateOutput)
instead of re-derived prose. `Background` = {task, context, plan,
assumptions}, persisted once per hop in workflow_state under companion key
`background.<task_id>` (best-effort on submit + every planner hop);
`intermediate_output` is NOT persisted — lives only in the envelope payload.
`build_user_prompt` gains labeled MESSAGE / BACKGROUND / INTERMEDIATE OUTPUT
sections behind `Settings.tuple_prompts` (default OFF; enabled only together
with arm D1). Workers without a tuple fall back to prose; arm C behavior is
byte-identical when off. Zero extra model calls.

## 16.5 design-discuss-harness.md (14.14 item 4 — BUILT with tests)

Multi-model debate as a coder harness; the PLAN is the debated object, the
product contract unchanged. Phases: Propose (every model in
`discuss_models`, parallel, per-call timeouts) -> Critique (one round,
failures skipped) -> Judge (`judge_model` returns `WINNER: <model>`;
unparseable output falls back deterministically to the first valid
proposal) -> Execute (`ToolLoopHarness` on the winner; summary prefixed
`[discuss] winner=<model>; ...`). Empty `discuss_models` degenerates to
`[model_coder]` (documented, tested). Cost: 2K+1 extra calls vs one — for
hard tasks only, not a default. Guardrails: no new authority path
(proposals/critiques are data, never executed); shared `coder_timeout_s`
budget; no pytest.mark decorators (repo content-filter rule).

## 16.6 design-learned-verifier.md (Flow-DPO step 2 — design, not built)

Why: deterministic checkers exist only for bench tasks; live verify-gate
relies on the critic alone (fabrication incident 2026-11-15: critic
approved an empty result). V1 (buildable now): cheap linear scorer —
features from the files dict (file count, bytes, lines, AST-parse flag,
comment fraction, duplicate ratio, placeholder detection: TODO/pass/empty/
fabricated-metric constants), logistic regression, pure-python optimizer,
weights as JSON artifact, `verify(files) -> score`. Honest scope: catches
SHAPE failures (fabrication), never semantic wrongness — complements, never
replaces, the checkers. V2: LoRA verifier on the shared base (Flow-DPO real
step, rung toward growing-LoRA), blocked on user compute approval + a
`record_solutions=True` hard-tier run (current 120-rep run has 0 recorded
solutions). Non-goals: no verifier output gates the bench loop (leakage
rule); no downloads without explicit approval; no sklearn/torch for V1.

## 16.7 docs/experience.md (Erfahrungsspeicher — folded into this file)

Was the curated "what we know about our models" store (framing
2026-11-14: shared Erfahrungspeicher — which models are good at what, which
task types to route where, which character traits emerge). Update rule:
every bench run / notable live task appends or amends an entry, evidence
required, contradicted entries corrected never deleted. Key entries
(2026-11-14, hypothesis unless noted):
- qwen2.5-coder:32b (coder, node-a): good at end-to-end coding through the
  debate loop (night3 live proof). 
- qwen2.5-coder:7b (planner, node-c): one "no JSON object found" failure
  (n=1); planning only, never coding.
- critic (7b, debate loop): rejects correct code — hallucinated blockers
  while deterministic checks passed; approves almost everything on
  proof-of-concept tasks (2 revisions over 40 live tasks); sycophantic
  under pressure (matches Du et al. 2023). Consequence: deterministic
  checks passing override critic rejection (worker.py ~1114).
Two layers: this roadmap file (distilled, versioned) + KV pheromones
(14.15.10, numeric hot layer). Full original file: git history.

## 16.8 One-line records (deleted, recoverable in git history)

- docs/architecture.md, docs/protocol.md, docs/operations.md,
  docs/recovery.md, docs/mesh-api-concept.md, docs/p2b_design.md —
  architectural/operational reference docs; superseded by plan.md +
  roadmap.md per cleanup decision.
- docs/bench/report-{easy-smoke,medium,hard}-2026-11-14.md,
  report-full-2026-11-15.md — seeded bench results (120-row full run:
  A 82.5% @ 609 tok/task, CIs and per-tier tables inside). Durable numbers
  live in section 15 and the experience notes above.
