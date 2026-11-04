# MEMNC — Distributed Multi-Agent Mesh over NATS

Implementation plan for `task.md`. Status: draft for review.

## 0. Vision delta — from homogeneous mesh to heterogeneous agent fabric

The core implementation (below, §1–§17) already delivers a correct, tested
**homogeneous** mesh: all workers run the same memnc binary against a local
inference runtime, coordinated by one orchestrator over NATS. That satisfies
the "NATS as transport + MCP as tool convention" base. This section records
what the *stated goal* actually adds on top — **agents on different machines
and different harnesses collaborating** — and the design decisions taken from
referenced work (hive, ormica via pypi, SwarmHarness arXiv:2605.28764, Cotal on
NATS, Synadia Agent Protocol, Internet-of-Agents 2407.07061).

### 0.1 What is missing in the homogeneous plan

| Capability | In §1–§17 today | Needed for the stated goal |
|---|---|---|
| Heterogeneous harnesses (Hermes, Claude Code, pi, own agent) | All workers are the same `memnc.worker` binary | A per-harness **adapter/bridge** so any agent that speaks MCP (or HTTP/SSE) can join the mesh unchanged |
| Capability discovery ("who can do X") | Roles are statically assigned (`planner/coder/critic`) | A **capability registry**: nodes advertise skills; callers discover them (SwarmHarness registry, Cotal presence-KV, Synadia `$SRV`) |
| Routing by suitability | Fixed role → fixed queue group | **Utility-based routing** over capability, load, latency, trust — needed once nodes differ in hardware/models/availability |
| Presence & liveness of heterogeneous nodes | `mesh.node.*.heartbeat` exists (model + busy) | Strengthen to full **presence KV with TTL** (idle/working/offline + capabilities), watched live for late joiners |
| Adding a new machine/harness | Manual: add replicas/env in compose | **Self-registration** — a node joins, advertises, starts receiving work |
| Ormica-style stigmergy | N/A | Optional: routing/trust signals act as **digital pheromones** (indirect coordination, no brittle message-passing) |

### 0.2 What we copy from each reference

**hive (aden-hive/hive).** The *Queen → Pilot → Workers* shape and the insight
that **orchestration is a tool call, not a compiled graph**: `run_worker` fans
subtasks out and returns immediately; the shared state is a **tracker** other
agents can query with SQL. → Lessons for memnc: keep the FSM, but let the
orchestrator *emit* subtasks as events any registered worker can pick up, and
expose the KV snapshot as a queryable ledger (closer to Cotal's presence KV +
`meshctl state`), rather than a fixed pipeline.

**ormica.** Three ideas transfer cleanly onto NATS:
- *Emergent hierarchy* — agents/roles spawned to meet demand rather than a
  fixed graph. NATS already gives this: anycast work queues let any number of
  role instances claim work.
- *Permission chain* — every sub-agent born passes a gate (AUTO/CHAIN/ROOT).
  → memnc maps this to the orchestrator's dispatch policy: only the
  orchestrator may dispatch, and only to roles it knows. Heterogeneous nodes
  must register via a capability grant, not self-declare authority.
- *Pheromone field (stigmergy) instead of message-passing* — agents post
  state/intent/progress to a shared field; others sense strong trails.
  → memnc's `mesh.node.*.heartbeat` + the workflow KV are already a weak
  pheromone field; SwarmHarness formalizes the same as **trust/credit scores
  feeding the router**.

**SwarmHarness (arXiv:2605.28764)** — the most directly on-point for the
heterogeneous distributed goal:
- *Skill-first nodes*: a node is a set of **skills** (typed folders) exposed
  simultaneously as HTTP and MCP — one source of truth, no dual maintenance.
  → memnc: the MCP tool server is the skill surface; a node's advertised
  skills = its MCP tool set + role.
- *SwarmRegistry (DHT)*: maps skill → capable nodes with TTL; churn handled by
  refresh, not leave messages. → memnc need not build a DHT in v1: NATS KV is
  a fine registry (Cotal does exactly this). DHT only if we go trustless.
- *SwarmRouter (utility scoring)*: `U(v,T)=w1·cap + w2·(1-load) + w3·(1-lat/dmax) + w4·trust`,
  argmax dispatch, multiple concurrent routers, submitter-signed attribution.
  → This is the routing upgrade for heterogeneous nodes; implement as a pure
  scorer on top of the presence KV.
- *Trust as digital pheromone*: nodes with positive credit get priority; idle
  nodes lose it. Optional for memnc but naturally an extension of the
  heartbeat.

**Cotal (on NATS) + Synadia Agent Protocol** — the practical, shipping anchors
and the closest match to Frank's own harnesses (Cotal ships connectors for
Claude Code, OpenCode, **Hermes**, and pi):
- *Shape = configuration*: peers, supervised tree, DAG pipeline are all just a
  subject/stream/account config, not code.
- *Three addressing modes* = NATS primitives: multicast = subject fan-out,
  unicast = per-agent durable inbox, anycast = JetStream work queue. Presence =
  a KV bucket with TTL + heartbeats; late joiners replay history from
  JetStream. **Reach for the NATS primitive before building your own.**
- *Sender identity rides the subject* (`cotal.<space>.inst.<target>.<sender>`)
  and the server polices it against a JWT; deny-by-default ACLs; fail-closed.
  → memnc should adopt subject-carried sender identity + token auth, and pin
  the per-agent durable inbox so nobody can impersonate the target.
- *Synadia Agent Protocol*: agents register as a NATS micro-service (`agents`),
  callers ping `$SRV.INFO.agents` for **discovery**, prompt on
  `agents.prompt.<agent>.<owner>.<name>`, stream typed chunks
  (response/status/query). → This is the *agent-facing* contract; Cotal is the
  *team* layer on top. memnc can adopt the same split.

### 0.3 Internet of Agents (IoA) — the open-loop check

IoA (arXiv:2407.07061) argues the three blockers of classic MAS are exactly
heterogeneity, single-device simulation, and rigid communication — and answers
with an agent-integration protocol + instant-messaging-shaped discovery and
dynamic teaming. Our design already removes #1 (NATS speaks to any harness via
an adapter + the Synadia/Cotal contract) and #2 (NATS is inherently
multi-machine/clustered). #3 (rigid pipelines) is addressed by Cotal's
shape-as-config and ormica's anycast + emergent roles, rather than IoA's
full-blown stateful conversation FSM, which we judge too heavy for v1.

### 0.4 Design decisions (adopted)

- **D1 — Adapter, not rewrite.** Heterogeneous harnesses join via a thin,
  per-harness **adapter** (a small NATS client that speaks the mesh contract on
  one side and the harness's native I/O — stdio MCP client, HTTP/SSE, CLI — on
  the other). No harness is modified. Reflection of Cotal's connectors.
- **D2 — Capability registry = NATS KV with TTL.** No DHT in v1. Nodes publish
  `{node, role, skills(MCP tool set), model, load, lat, state}` to a
  `mesh.registry` KV bucket (TTL ~30 s, refreshed by heartbeat). Any peer/Cotal-
  style watcher sees who is online and capable. DHT (SwarmHarness) is a later,
  trustless option.
- **D3 — Keep the orchestrator, make routing utility-scored.** The FSM +
  singleton orchestrator is sound and tested (§6). Extend selection: role →
  candidate nodes (registry) → `max U(v,T)` over load/latency/trust. Multiple
  orchestrator *routers* can exist later; attribution stays with the submitter.
- **D4 — Subject-carried sender identity + token auth.** Adopt
  `mesh.<space>.<mode>.<target>.<sender>` with the server policing identity
  against a JWT (deny-by-default), not trusting an identity field in the JSON.
- **D5 — Presence KV is the pheromone field.** Reuse the existing
  `mesh.node.*.heartbeat` as writes into a TTL KV bucket; the router and
  `meshctl` read it. Late joiners discover by reading/watching the bucket.
- **D6 — MCP is the shared skill surface.** Both directions: a worker *exposes*
  its tools over MCP; a driver/harness *consumes* another agent's MCP/HTTP
  surface through the adapter. Matches HarnessAPI's "typed skill folder as both
  HTTP and MCP" and keeps memnc harness-agnostic.

### 0.5 Phased next steps (→ §14 Milestones M7–M9)

- **M7 Heterogeneous adapter + capability registry.** `memnc.agent` adapter
  that lets an external harness (start with **Hermes**/**pi** via a Cotal-style
  connector contract, then Claude Code) join the mesh: register capabilities in
  `mesh.registry` KV, receive anycast work, reply. Registry visible in
  `meshctl` (→ **M7-CLR**: add a `meshctl registry` command that lists
  advertised capabilities; currently missing).
- **M8 Utility-based routing + presence + trust.** Router scores candidates
  `U = w1·cap + w2·(1−load) + w3·(1−lat) + w4·trust`; heartbeat→presence KV with
  TTL; `meshctl` shows live roster + load. Multi-machine diagram in §3.
  **Add (SwarmHarness 2605.28764):** (a) *trust ledger* — how credit is
  accrued/lost (successful replies raise the credit of the responder; idle
  nodes decay; submitter sets `w4`), stored in a `mesh.trust` KV with TTL
  and read by the router; (b) *submitter-signed attribution* — each routed work
  records its submitter so multiple concurrent routers can coexist and
  attribution stays with the submitter (D3), not the one who routed last;
  (c) *multi-router support* — the scorer is a pure function over the presence
  + trust KV so >1 router instance can run.
- **M9 Subject-carried identity + ACLs + capability gate.** NATS token →
  per-agent JWT/users ACLs; sender token policed on the wire; pinned per-agent
  durable inbox; document in §5.2 and architecture.md. **Add (ormica):** the
  *capability gate* — a heterogeneous node may only join if it passes a grant
  from the orchestrator (or a trusted root), not by self-declaring authority;
  maps the permission-chain (AUTO/CHAIN/ROOT) onto the registry so the
  `mesh.registry` write is authorized, deny-by-default.
- **M10 Queryable mesh ledger + fan-out tool-call (hive).** Two hive lessons
  not yet milestone'd: (a) *tracker* — expose registry + presence + task state
  as a queryable ledger any agent can read (what `meshctl state`/`registry`
  show), so coordinating agents sense mesh state instead of ad-hoc messages
  (weak pheromone field, D5); (b) *fan-out as a tool call* — an orchestrator
  emits subtasks as events (`mesh.task.submit` fan-out) and returns
  immediately; any registered worker claims them via the anycast queue (the
  `run_worker`-shaped split, already scaffolded by M7/M8). Distinct from M8's
  per-task routing; this is the role->subtask decomposition entry point.



## 1. Goal

A lightweight, horizontally scalable multi-agent system where:

- NATS (Docker) is the only transport: pub/sub for events, request/reply for task submission and health.
- A state-machine orchestrator assigns roles (`planner`, `coder`, `critic`), tracks each task's state durably, and routes payloads.
- Each worker node runs local MCP tool servers (filesystem + shell) and talks to a **local inference runtime** (Ollama or any OpenAI-compatible endpoint).
- No agent framework, no cloud services, no LangChain/CrewAI/etc. Python + NATS + HTTP only.

## 2. Non-goals

- No web UI in v1 (`meshctl` CLI + event log instead).
- No built-in model serving: the repo **connects** to inference endpoints; it does not ship Ollama or GPU configs.
- No multi-tenant auth beyond a NATS token and optional inference API keys.
- No exactly-once semantics. We target at-least-once with idempotent checkpoints (see §7.4).

## 3. Architecture

```
             ┌──────────────────┐
 meshctl ───▶│  NATS (Docker)   │◀──────────────┐
             │  core pub/sub    │               │
             │  JetStream + KV  │               │
             │  request/reply   │               │
             └───┬──────────┬───┘               │
                 │          │                   │
   mesh.agent.*.cmd        │ mesh.agent.*.result│
                 ▼          ▼                   │
        ┌──────────────────────────┐            │
        │  Orchestrator (1)        │            │
        │  FSM + state snapshots   │            │
        └──────────────────────────┘            │
                 ▲                               │
     ┌───────────┼───────────┐                   │
     │           │           │                   │
┌─────────┐ ┌─────────┐ ┌─────────┐              │
│ planner │ │  coder  │ │ critic  │  workers ────┘
│ worker  │ │ worker  │ │ worker  │
└────┬────┘ └────┬────┘ └────┬────┘
     │ stdio     │ stdio     │ stdio
┌────▼────┐ ┌────▼────┐ ┌────▼────┐        HTTP
│MCP srv  │ │MCP srv  │ │MCP srv  │──────▶ Ollama / OpenAI-compatible
│fs+shell │ │fs+shell │ │fs+shell │        (local to each node)
└─────────┘ └─────────┘ └─────────┘
```

Node model: a node is one `docker-compose` stack (or bare `python -m memnc.worker`) that joins a shared `NATS_URL` and points at its own `INFERENCE_BASE_URL`. Adding a node = adding replicas/env, no code change.

## 4. Stack

| Concern | Choice | Why |
|---|---|---|
| Language | Python 3.12, asyncio | Minimal, matches requirement |
| NATS client | `nats-py` | Official, async, JetStream support |
| MCP | `mcp` Python SDK (`FastMCP`) | stdio transport out of the box |
| HTTP | `httpx` | One client for Ollama + OpenAI-compatible |
| Config | `pydantic-settings` | Env-driven per node |
| Tests | `pytest`, `pytest-asyncio` | Standard |
| Packaging | `pyproject.toml`, no build backend complexity | Single installable package |
| Container | `python:3.12-slim`, non-root | Small image |

No framework imports in the core path: `fsm.py` is pure functions, `worker.py` is a loop.

## 5. Wire protocol

### 5.1 Envelope

Every message on the mesh is this shape (defined once in `schema.py`, validated on receive):

```json
{
  "v": 1,
  "id": "01J8Z...",
  "task_id": "t_01J8Z...",
  "trace_id": "01J8Z...",
  "kind": "command | result | event | heartbeat",
  "role": "planner | coder | critic",
  "node": "node-a",
  "state": "CODING",
  "attempt": 1,
  "deadline_ms": 300000,
  "payload": {},
  "ts": "2026-10-31T18:00:00Z"
}
```

`payload` is role-specific:

- planner result: `{ "plan": [ { "step": 1, "description": "...", "files": [] } ], "notes": "..." }`
- coder result: `{ "summary": "...", "files": [ { "path": "...", "content": "..." } ], "commands": [] }`
- critic result: `{ "approved": false, "severity": "major", "issues": [ { "file": "...", "line": 12, "issue": "...", "fix": "..." } ] }`

### 5.2 Subjects

| Subject | Pattern | Purpose |
|---|---|---|
| `mesh.task.submit` | request/reply | Submit a task, returns `{task_id}` or validation error |
| `mesh.task.<id>.events` | pub/sub | Append-only timeline (state changes, results, errors) |
| `mesh.agent.<role>.cmd` | queue group `q-<role>` | Work assignment |
| `mesh.agent.<role>.result` | pub/sub | Worker output |
| `mesh.node.<node>.heartbeat` | pub/sub | Liveness + role + model + queue depth |
| `mesh.dlq` | pub/sub | Exhausted or unparseable messages |

### 5.3 Delivery semantics

- Stream `MESH` on `mesh.>`: `max_age=7d`, `max_msgs=1M`, `duplicate_window=120s`, dedupe via `Nats-Msg-Id = envelope.id`.
- Durable pull consumers:
  - `worker-<role>` → filter `mesh.agent.<role>.cmd`, explicit ack, `max_deliver=3`, `ack_wait` = 1.5× role timeout.
  - `orchestrator-results` → filter `mesh.agent.*.result`, explicit ack.
- KV bucket `workflow_state`: key `task.<id>` → snapshot, `history=10`.
- Every state transition and result also publishes to `mesh.task.<id>.events` for replay/trace.

## 6. Orchestrator

### 6.1 State machine

Pure function `transition(state, event, ctx) -> (state, actions)` in `fsm.py`; no I/O, fully unit-tested. The runtime applies actions (`dispatch`, `retry`, `finish`, `escalate`) by calling `bus`/`store`.

```
RECEIVED ──▶ PLANNING ──▶ CODING ──▶ CRITIQUE ──approved──▶ DONE
                ▲            ▲            │
                │            └──REVISE◀───┘ rejected (revisions < max)
                │
                └── retry (attempts < max)

any state ──timeout/cancel/exhausted──▶ FAILED | CANCELLED
```

| From | Event | Guard | To | Action |
|---|---|---|---|---|
| RECEIVED | `task.accepted` | valid schema | PLANNING | dispatch(planner) |
| PLANNING | `result.ok` | non-empty plan | CODING | dispatch(coder) |
| PLANNING | `result.fail` / `timeout` | attempt < max | PLANNING | retry(planner) |
| PLANNING | failure | attempt ≥ max | FAILED | escalate |
| CODING | `result.ok` | — | CRITIQUE | dispatch(critic) |
| CODING | `result.fail` / `timeout` | attempt < max | CODING | retry(coder) |
| CRITIQUE | `result.ok`, `approved=true` | — | DONE | finish |
| CRITIQUE | `result.ok`, `approved=false` | revisions < max | REVISE | dispatch(coder, issues) |
| CRITIQUE | rejected | revisions ≥ max | FAILED | escalate |
| any | `task.cancel` | — | CANCELLED | terminate |

Defaults: `MAX_REVISIONS=3`, `MAX_ATTEMPTS=3`, per-role timeouts (planner 120s, coder 300s, critic 120s), `MAX_TOOL_STEPS=12`.

### 6.2 Persistence & recovery

- Snapshot written **before** dispatch (intent) and **after** result (outcome), so a crash always has a recoverable state.
- On startup the orchestrator rebuilds active tasks from KV and re-dispatches anything whose last action is unanswered (uses `attempt` + `deadline` to avoid double-dispatch).
- Duplicate results (same `id`) are dropped by checking `snap.seen_result_ids`.
- Exhausted/undoable messages go to `mesh.dlq` with the original envelope + reason.

## 7. Workers

### 7.1 Process model

One generic worker binary, role injected via config:

```bash
python -m memnc.worker --role coder --node node-a
```

Subscribes to its role's durable consumer, processes one task at a time per slot (`MAX_CONCURRENCY`, default 1 — inference is the bottleneck), heartbeats every 5s.

### 7.2 Task loop

1. Fetch message, load snapshot from KV.
2. If `command.id` already in `snap.completed_commands`, ack and skip (dedupe).
3. Render role prompt + context: original task, plan, prior code, prior critique, revision notes.
4. Tool loop (max `MAX_TOOL_STEPS`):
   - `backend.chat(messages, tools=mcp.tool_schema)`
   - no tool calls → final answer
   - tool calls → execute via MCP, append results, checkpoint each call
5. Validate the role's output schema; publish `result.ok` or `result.fail`; ack.
6. On timeout/crash before ack, JetStream redelivers; snapshot prevents re-running finished tool calls.

### 7.3 Roles (`roles.py`)

- `planner` — decompose task into ordered steps, name target files, no code.
- `coder` — emit full file contents and optional shell commands; honors critique issues on revision.
- `critic` — returns only the strict JSON verdict (`approved`, issues); never rewrites code.

Prompts are versioned strings; each result records `prompt_version` for reproducibility.

### 7.4 Idempotency caveat

Tool calls are checkpointed by `(command_id, tool, args_hash)` in the snapshot. Finished calls are never re-executed on redelivery. A command killed *mid-execution* may still re-run — accepted risk of at-least-once with full shell; documented in `docs/protocol.md`.

## 8. MCP tool servers

`memnc.mcp.server` runs as a **stdio subprocess per worker** and exposes:

| Tool | Signature | Notes |
|---|---|---|
| `read_file` | `(path, max_bytes=200_000)` | Confined to `WORKSPACE_ROOT` |
| `write_file` | `(path, content)` | Creates parents; path-confined |
| `list_dir` | `(path=".", depth=2)` | Path-confined |
| `search_text` | `(pattern, glob="**/*", max_results=100)` | Path-confined |
| `run_shell` | `(cmd, cwd=".", timeout_s=120)` | **Full shell, no allow/deny list** |

`run_shell` keeps only operational limits: timeout, output truncation (64KB), cwd default, per-call audit log line (`task_id`, `node`, `cmd`, exit code, duration). The isolation boundary is the container, per decision:

- non-root uid, workspace volume only, no Docker socket, CPU/mem/PID limits
- workers share a dedicated Docker network; NATS is the only required egress
- documented consequence: the model can execute arbitrary code and reach anything the worker container can reach

`docs/protocol.md` documents tool schemas as structured JSON, and a `tests/test_tools.py` suite covers path-escape attempts, timeouts, and output caps.

## 9. Inference backends

Providers are pluggable; the repo ships clients, not runtimes.

```python
class ChatBackend(Protocol):
    async def chat(self, messages, tools, model, **opts) -> ChatResult  # content + ToolCall[]
    async def list_models(self) -> list[str]
```

| Provider | Endpoint | Mapping |
|---|---|---|
| `ollama` | `POST {base}/api/chat` | native `tools`, `keep_alive`, `num_ctx`, `num_predict`, `stream=false` |
| `openai` | `POST {base}/v1/chat/completions` | `tool_calls`, bearer key, optional org header |

Both normalize to `ToolCall(name, args, id)` so the worker loop is provider-agnostic. Shared behavior: connect/read timeouts, exponential backoff on 429/5xx (max 3), no retry on 4xx, token/step cap.

Config (all env, per node):

```
INFERENCE_PROVIDER=ollama|openai
INFERENCE_BASE_URL=http://host.docker.internal:11434   # or LAN/remote URL
INFERENCE_API_KEY=                                     # openai-compatible only
INFERENCE_TIMEOUT_S=180
MODEL_PLANNER=qwen2.5:7b
MODEL_CODER=qwen2.5-coder:7b
MODEL_CRITIC=llama3.1:8b
```

`meshctl doctor` probes `/api/tags` or `/v1/models` and reports missing models + latency; it warns instead of blocking task submission.

## 10. Deployment

### 10.1 Compose services

| Service | Image | Role |
|---|---|---|
| `nats` | `nats:2-alpine` | JetStream, monitoring `:8222`, token auth |
| `orchestrator` | `memnc` | 1 replica |
| `worker-planner` | `memnc` | `ROLE=planner` |
| `worker-coder` | `memnc` | `ROLE=coder` |
| `worker-critic` | `memnc` | `ROLE=critic` |

Scale out with `docker compose up --scale worker-coder=3`; queue groups guarantee one delivery per command. Multi-host: run the worker services on each host with a shared external `NATS_URL`; a `docker-compose.node.yml` ships for that.

NatS config (`docker/nats/nats.conf`):

```conf
port: 4222
http_port: 8222
max_payload: 4MB
jetstream { store_dir: /data, max_mem_store: 256MB, max_file_store: 2GB }
authorization { token: $NATS_TOKEN }
```

Note: NATS config can't interpolate env directly — entrypoint script renders it, or use `-js -sd /data --auth $NATS_TOKEN` flags in the compose command. Plan: compose command flags, no templating.

### 10.2 Worker container

`python:3.12-slim`, package installed with `pip install --no-cache-dir .`, `useradd -u 10001`, `USER 10001`, workspace at `/workspace`, `read_only: true` root FS with `tmpfs: /tmp`, `cap_drop: [ALL]`, `security_opt: [no-new-privileges]`, CPU/mem/PID limits.

### 10.3 Environment

`.env.example` documents every variable: `NATS_URL`, `NATS_TOKEN`, `ROLE`, `NODE_NAME`, `WORKSPACE_ROOT`, `MAX_CONCURRENCY`, `MAX_REVISIONS`, `MAX_ATTEMPTS`, `MAX_TOOL_STEPS`, `ALLOW_SHELL` (default `1` per decision), inference vars, `LOG_LEVEL`.

## 11. Repo layout

```
pyproject.toml
docker-compose.yml
docker-compose.node.yml
.env.example
docker/
  worker.Dockerfile
  nats/nats.conf
src/memnc/
  __init__.py
  config.py        # pydantic-settings, env
  schema.py        # envelope, payload models, validation
  bus.py           # NATS connect, JetStream publish/consume, KV, dedupe
  fsm.py           # pure transitions + guards
  orchestrator.py  # runtime around fsm: store, dispatch, recovery, DLQ
  worker.py        # pull loop, prompts, tool loop, checkpointing
  roles.py         # prompt templates + output schemas
  backends/
    __init__.py    # ChatBackend protocol, factory
    ollama.py
    openai.py
  mcp/
    server.py      # FastMCP tools (stdio entrypoint)
    client.py      # session lifecycle, tool schema cache
    tools_fs.py
    tools_shell.py
  cli.py           # meshctl submit|tail|doctor|state
tests/
  test_fsm.py test_schema.py test_bus.py
  test_tools.py test_worker_loop.py test_recovery.py test_backends.py
docs/
  architecture.md protocol.md operations.md
scripts/
  smoke.sh
```

## 12. Observability

- Structured JSON logs to stdout (`ts`, `level`, `node`, `role`, `task_id`, `trace_id`, `event`, `duration_ms`).
- `meshctl submit "..."` → prints `task_id`.
- `meshctl tail <task_id>` → subscribes to `mesh.task.<id>.events`, pretty-prints the timeline.
- `meshctl state <task_id>` → dumps the KV snapshot.
- `meshctl doctor` → NATS connectivity, JetStream enabled, KV bucket present, inference reachable, models present.
- `mesh.node.*.heartbeat` exposes role, model, queue depth, current task.

## 13. Testing

| Level | What | How |
|---|---|---|
| Unit | FSM transitions, guards, revision/admission limits | `pytest`, table-driven, no I/O |
| Unit | Envelope/payload validation, payload schema per role | `pydantic` round-trips |
| Unit | MCP tools: path escape, timeout, output cap, audit line | tmp workspace |
| Unit | Backend parsing (Ollama/OpenAI tool-call shapes) | recorded JSON fixtures |
| Integration | Full loop without LLM | echo backend, real NATS in Docker, task reaches DONE |
| Integration | Revision loop | scripted backend rejects twice then approves |
| Recovery | Kill worker mid-task, redelivery, no duplicate tool exec | snapshot assertions |
| Recovery | Restart orchestrator, active task resumes | KV rebuild |
| E2E (opt-in) | Real model run | `scripts/smoke.sh` with `INFERENCE_BASE_URL` |

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
| M7 | Heterogeneous adapter + capability registry (§0.5) | An external harness (Hermes/pi, then Claude Code) joins the mesh via `memnc.agent`, advertises skills in `mesh.registry` KV, receives anycast work, replies; **adapter+registry done (commit 374fa3e); meshctl registry view is the remaining M7-CLR clearance** |
| M8 | Utility routing + presence + trust (§0.5) | Router scores `U=w1·cap+w2·(1−load)+w3·(1−lat)+w4·trust` over presence KV (TTL); `meshctl` shows live roster + load; **trust ledger, submitter-signed attribution, >1 concurrent router work (SwarmHarness)**; multi-machine topology documented in §3; **trust ledger + utility scorer done (commit 03f1f35); live router on NATS + submitter-signed attribution is the remaining M8 clearance** |
| M9 | Subject identity + ACLs + capability gate (§0.5) | NATS token → per-agent JWT/users ACLs; sender token policed on the wire; pinned per-agent durable inbox; **registry write authorized via orchestrator grant, deny-by-default (ormica permission-chain)**; documented in §5.2 + architecture.md; **grant ledger + gate (acl.py, deny-by-default) + meshctl grant/grants/revoke done (M9); LIVE GATED 2-NODE RUN PASSED 2026-11-02: node B ([REDACTED], maco) deployed via docker-compose.node.yml against node A NATS, heartbeat-bootstrap grant + registry verified, meshctl submit completed end-to-end across node-a/node-b planner/coder/critic (task.done); broker-side JWT/user-ACL config remains optional hardening** |
| M10 | Queryable mesh ledger + fan-out tool-call (§0.5) | `meshctl registry` + presence/state readable as a ledger any agent queries; orchestrator emits subtasks as `mesh.task.submit` fan-out and returns immediately, anycast workers claim them (hive run_worker shape); **DONE 2026-11-02: `mesh.ledger` request/reply + `meshctl ledger` (orchestrator answers, Bus fallback) + `submit --fanout` decomposition (≥2 plan steps → independent subtasks, parent parks in CODING, `_check_fanouts` aggregates DONE/FAILED); live: 2 subtasks emitted on submit, 1 DONE / 1 failed (worker tool-step limit, not M10), aggregation correct; broker-side JWT/user-ACL remains optional hardening** |

Each milestone is independently demoable; M2 is the checkpoint that proves the mesh before any model is involved.

### 14.1 Roadmap: symmetrisches 2-Modell-Self-Orchestration (2026-11-02)

Ziel: Beide Nodes sind **architektonisch identisch** — jeder Node kann submit, planen, claimen, codeen, kritisieren und fan-out. Keine feste Rolle, kein "Controller-Node". Der Orchestrator wird schrittweise von Dispatcher zu optionalen Schiedsrichter degradiert (M11 Richtung, fertiggestellt in M14).

| Step | Inhalt | Done-Kriterium |
|---|---|---|
| **M12 — M11 live, beide Nodes** | Node A mit M11-Code rebuilden; Node B (maco) auf aktuellen Stand heben (`docker-compose.node.yml`); `mesh_claim=true` auf Node-B-Workern; Live-Test: `meshctl submit --claimable` → Node-B-Worker claimt via CAS und läuft die Pipeline | Task submitted auf A, geclaimt+DONE von B; zweiter Run umgekehrt (B submitted → A claimt) | **[DONE 2026-11-02]**
| **M13 — Agent als Submitter live** | Coder/Planner-Agent ruft `mesh_submit` aus einem laufenden Task heraus; Subtask landet im Mesh und wird von der *anderen* Node geclaimt | Nachweis: Task auf A erzeugt Subtask, B modelarbeitet ihn ab (und umgekehrt); `meshctl ledger` zeigt beide Nodes als Submitter+Claimer | **[DONE 2026-11-02]**
| **M14 — Orchestrator-los dispatchen** | Claimable Tasks bekommen `next_role`; nur die passende Rolle kann claimen (CAS-Rollen-Gate); der claimende Worker fährt die Pipeline **selbst** weiter (FSM-`transition` wiederverwendet) und macht den nächsten Schritt claimable (RECEIVED→PLANNING→CODING→CRITIQUE→DONE). Orchestrator bleibt nur Zuschauer; `_dispatch_claimed` optional | Submit mit gestopptem Orchestrator: Task wird trotzdem geclaimt und ausgeführt; Orchestrator-Neustart aggregiert nur noch | **[CODE DONE 2026-11-02, commit b49e142: next_role-Feld, role-gated CAS-Claim, Worker startet Pipeline selbst (FSM-`transition` wiederverwendet), Orchestrator überspringt worker-driven Tasks; 124 Unit-Tests grün. LIVE-TEST NOCH OFFEN: Images deployen, claimable Submission, Pipeline ohne Orchestrator-Dispatch verifizieren]**
| **M15 — Symmetrische Topologie** | Node B betreibt eigenen Orchestrator (nur für eigene Submissions/Fan-out zuständig); ein Broker (NATS A) oder Leaf-Nodes; beide Nodes konfig-identisch außer Modellen | Beide Nodes submitted Fan-out-Tasks gleichzeitig ohne Kollision (CAS/KV schützt); Doku §3 aktualisiert | **[CODE DONE 2026-11-02/03, commits 86fc593 + 928e2ed: owner-routed Submit-Subjects, origin_node, Ownership-Guards, Worker-side Fanout-Split, Node-B-Orchestrator-Service, Wildcard-Submit-Subscription + per-Node-Durable-Names + Legacy-node-a-Akzeptanz; LIVE-TEST NOCH OFFEN: simultaner Fan-out-Run beider Nodes]**
| **M16 — Demo: echtes Peer-Orchestration** | Ein Task wird auf B gestellt, B plant, fan-out landet symmetrisch auf A+B, beide Modelle (unterschiedliche Ollamas) arbeiten gegenseitig ab, Cross-Node-Critique | End-to-end-Run mit Wechselricht ×2 (A→B und B→A Anteile), `task.done` mit beidseitiger Attribution im Ledger |

Nicht-Blocker (optional danach): Broker-JWT/ACL-Hardening (M9-Rest), `MAX_TOOL_STEPS` für den 3B-Coder erhöhen, Trust-Utility in die Claim-Entscheidung (M8-Score beim Claim-Poll).

### 14.2 Ausbau-Stufe: Smoke-Gate, UI, Human-Steering (2026-11-03)

*Status: S1 ist implementiert und grün (`39be633`). S2–S4 sind reine Planung — nichts davon ist gebaut. Auch die Ausblicks-Abschnitte 14.3/14.4 sind Vision, nicht Roadmap mit Termin.*

Diese Stufe kommt **nach** M16 (echtes Peer-Orchestration live) und macht aus dem Mesh ein täglich benutzbares Werkzeug. Reihenfolge bewusst so: erst das Gate (S1), dann die Modelle (S2), dann Sichtbarkeit (S3), dann Kontrolle (S4).

| Step | Inhalt | Done-Kriterium |
|---|---|---|
| **S1 — Smoke-Gate** | `scripts/smoke.sh` hart machen: submit → `state` pollen bis DONE/FAILED mit Timeout (statt blindes `tail --limit`); Assert auf Endzustand DONE, Exit-Code 0/1 (CI-tauglich); Modi `--claimable` (Referee-Modus: Orchestrator nimmt an, verteilt aber nicht — Worker mit `MESH_CLAIM=true` claimen selbst = M14-Nachweis) und `--fanout`; lauffähig gegen Ollama- UND OpenAI-Backend (`INFERENCE_PROVIDER`-Parameter) | `./scripts/smoke.sh` endet grün auf maco und auf .180; roter Exit, wenn Task FAILED/Timeout; Smoke für M14/M15/M16-Live-Tests wiederverwendet |

**S1-Ergebnis (2026-11-04):** Grün auf maco (Pipeline PLANNING→CODING→CRITIQUE→DONE in 39 s, Commit `39be633`). Beobachtungen: mannix-Tool-Schema-Zittern (4/7 Calls mit falschen Argumentnamen), Critic-Laxheit (`hello from memnc` statt `hello from the mesh` passierte) — beide S2/S4-Relevanz. Auf .180 verhielt sich das Gate korrekt rot: llama3.2:3b versuchte 6/7 Tool-Calls mit falscher Argument-Verschachtelung (`p:` statt `pattern:`, `f:` statt `path:`) und schrieb eine 6-Byte-truncated hello.py → FAILED/Exit 1 (Gate-Verhalten korrekt); qwen2.5-coder:7b failed im Planner („no JSON object found"), während ein Direkt-Ollama-Probe mit Minimalprompt sofort perfektes JSON lieferte → Problem ist die lange Planner-Prompt/Context-Situation auf der CPU-Box, nicht das Modell generell. **M14 `--claimable` live: ausstehend** — Submittieren funktioniert (Task landet RECEIVED/claimable), aber kein Claim, weil `MESH_CLAIM=false` auf allen Workern beider Nodes; Nächster Schritt: `MESH_CLAIM=true` auf .180-Worker-Compose, Smoke `--claimable` wiederholen, Claim-Transitions (claimed_by/node-b) als Nachweis dokumentieren.
| **S2 — Swift als Default-Backend** | Worker-Compose auf `INFERENCE_PROVIDER=openai`, `INFERENCE_BASE_URL=http://host.docker.internal:8097/v1`, `MODEL_*` auf Swift-1.5 (Qwen3.8-27B GSQ, llama-server :8097); mannix 27b wird vom 6800XT abgeladen (VRAM: Swift ~13 GB + mannix 16 GB passen nicht zusammen); mannix bleibt Reservemodell für Diskussionsrunden | Smoke (S1) grün mit Swift; `meshctl doctor` zeigt Swift-Latenz; keine Thinking-Leaks (Swift denkt kurz, num_ctx-Budget hält) |
| **S3 — UI (Mesh-Console)** | Lokale Web-Console (kleiner HTTP-Server, kein Framework-Zwang): Task-Liste + State-Farben, Ledger-Sicht (Registry/Grants/Trust), Live-`tail` (SSE oder Poll), Node-Roster mit Load/Latenz; read-only — schreibt nichts | `http://localhost:<port>` zeigt live denselben Zustand wie `meshctl ledger`/`tasks`; funktioniert von beiden Nodes aus (Node-B rendert gegen eigenen Broker) |
| **S4 — Human-in-the-Loop (Steering)** | Approval-Gate im Critique-Schritt: Task parkt in CRITIQUE mit `needs_human=true`-Flag (KV), Console/Telegram zeigt Diff+Verdict, Human approved/rejected → FSM läuft weiter; Telegram-Bot als zweites Interface (submit, approve, tail); Grants bleiben die Autorisierungs-Ebene (M9) | End-to-end: Task mit kritikwürdigem Patch parkt, Human lehnt ab → Revision-Loop, approved → DONE; alles auch aus Telegram bedienbar |

Abgrenzung: S4 ersetzt keine M9-ACLs (Maschinen-Ebene) und keine Trust-Scores (statistische Ebene) — das Human-Veto ist die dritte Ebene darüber.

### 14.3 Ausblick: Selbstorganisation & soziale Kompetenz (2026-11-03)

Zwei Kernelemente tragen das Mesh-Konzept über DeepSeeds dsh-superagent hinaus (deren Panel zeigt nur „what needs a person" — der Mensch im Loop; unsere Agenten organisieren sich zusätzlich *untereinander*):

**Selbstorganisation** — der Mesh verteilt sich selbst, kein zentraler Dispatcher:
- *schon real:* CAS-Claiming cross-node (M11/M12), Agent-Subtasks über Hierarchie-Ebenen (M13), Owner-Routing + Fanout (M15)
- *Ausbau:* **dynamische Rollen** — die Diskussion (disc_20261103_230643) kam zum Schluss, dass feste Leader-Follower-Rollen bei async Latenz Deadlocks erzeugen; zustandsbasierte Koordination + Capability-Hierarchie stattdessen. **Trust-Score in die Claim-Entscheidung** (M8-Score beim Claim-Poll): zuverlässige Nodes erben attraktivere Tasks automatisch — Reputations-getriebene Selbstorganisation.

**Soziale Kompetenz** — Verhandlung statt Befehlsausführung:
- *Modell↔Modell:* Peer-Diskussion mit fachlichem Widerspruch und Selbstbeendigung ([DONE], disc_20261103_230643) als nachweisbares Verhandlungsmuster; Critique-Schritt als institutionalisiertes Peer-Review; Registry/Capabilities als soziales Protokoll („wer kann was")
- *Modell↔Mensch:* Trust-Ledger als soziales Gedächtnis, Grants als Autoritätsinstanz (M9), Approval-Gate (S4) als Human-Veto
- *Ausbau:* Task-gesteuerte Konsensfindung — kritische Tasks dürfen vor Ausführung eine Modell-Debatte anstoßen (die Diskussions-Harness wird zum Mesh-Primitiv); Verdict-Empfehlung fließt in den Trust-Score ein.

**Design-Konsequenz für S3:** Die Console ist Beobachtungsfenster der Selbstorganisation, nicht Dashboard: sie zeigt *wer sich wie organisiert* (Claims, Rollen, Node-Roster) und *wie die Verhandlung steht* (Critique-Verdicts, Trust, Grants) — nicht nur eine Task-Liste.

### 14.4 Ausblick: Task-Affinität, Evidenz-Metriken & gelernte Verteilung (2026-11-04)

**Task-Affinität / „Task nicht wegnehmen"** (aus DeepSeek dsh-superagent-Prototype, S. 2026-11-03):
- Ist ein Task an ein Modell verteilt, sollte er ihm nicht sofort wieder entzogen werden — Modelle tendieren („ich kann das besser"), aber Umverteilung ist ineffizient: der Empfangs-Kontext (Prefill, Verlauf, Workspace-State) muss neu aufgebaut werden. DeepSeek löst das per **Long-Term-KV-Cache**; unser Pendant: Task-Stabilität auf Workflow-Ebene.
- *Status:* teilweise drin — `origin_node`-Routing (M15) bindet Tasks an die Owner-Node, Claims sind hart (CAS), Critique-Loops bleiben beim selben Worker. **Nicht drin:** Affinität auf Modell-Ebene. Sobald Cloud-Modelle ins Mesh kommen (S2-Variante mit externer API), wird Cache-Hit zum Kosten-/Latenzfaktor erster Ordnung: Umverteilung = voller Prefill = volle Token-Zahlung; Verbleib auf der Model-Instanz = Cache-Hit.
- *Ausbau:* Routing berücksichtigt `(node, model_id)`-Affinität — wiederaufgenommene Tasks/Follow-ups bevorzugen die Instanz, die den Kontext schon im Cache hält. Hot-Standby-Instanzen pro Rolle (Swift/Ollama) als Cache-Träger.

**Evidenz-Metriken für Selbstorganisation** (Idee F., 2026-11-04; Referenz Graphify-Labs/graphify):
- Selbstorganisation ist ohne Messung nicht steuerbar. Wir brauchen eine Effizienz-Benchmark: **naive Tool-Call-Schleife vs. strukturierte Suche** (Graph-/Retrieval-unterstützt) auf identischen Tasks — gemessen an: Tokens/Task, **Failed-Tool-Call-Ratio** (beim S1-Lauf: 4/7 mannix-Calls fehlerhaft), Zeit-zu-DONE, Critique-Roundtrips.
- Graphify-Benchmark-Muster übernehmen: fixed agent, feste Fragen auf realer Codebasis (z. B. memnc selbst, ~1M-LOC-Referenz ERPNext als Benchmark-Vorbild: grep-Baseline 70,8% vs. graph 82,0% Key-Fact-Coverage), exakte Grader statt LLM-Judge. Unabhängige Replikationen warnen: Struktur-Vorteil konzentriert sich auf Multi-hop-Queries; einfache Lookups gewinnt grep — der Mesh-Metric muss also **pro Task-Typ** messen, nicht als ein Zahl.
- Mündet in Mesh-Primitiv: Task-Kontext (Plan, Workspace-State, relevantes Code-Graph) als queried Ressource statt als re-readen Kontext.

**Selbstlernende Verteilung (ant-based):**
- Aktuell: statisches Claim-Polling + Owner-Routing. Später: Verteilung als Stigmergie — Tasks hinterlassen Pheromon-artige Spuren (Erfolgs-/Kosten-Signale pro `(node, model, task_typ)`), Trust-Score + Affinität werden daraus laufend aktualisiert statt konfiguriert. Ameisen-Algorithmus als Bewertungsschema für den Claim-Wettlauf; Startpunkt sind die schon gemessenen Evidenz-Metriken als Signalfunktion.

**Rollen-Finetuning (LoRA):**
- Modelle mit LoRA auf sozialer Interaktion und Rollenverhalten finetunen (Planner/Coder/Critic als Verhaltensstile, nicht nur Prompt-Rollen) — passt zur Memory-Vision (Growing-LoRA, Fisher-Merge): die Mesh-Rollen werden trainierbare Model-Zustände; der Critic lernt aus S1-artigen Läufen, wann Strictness gerechtfertigt ist (siehe Critic-Laxheit im ersten S1-Lauf).

## 15. Risks & mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Full shell = arbitrary code exec | High | Container-only isolation: non-root, workspace-only mount, no socket, caps dropped, resource limits; documented |
| At-least-once redelivery re-runs shell | Medium | Checkpoint `(command_id, tool, args_hash)`; skip completed calls; document mid-execution gap |
| Model ignores tool/JSON contract | Medium | Strict schemas + one repair retry + fallback JSON action protocol; fail the task, don't loop |
| Weak local models produce poor critique | Medium | Structured verdict only; `MAX_REVISIONS` cap; per-role model config |
| iGPU concurrency thrash | Medium | `MAX_CONCURRENCY=1` per worker, queue groups spread load, `keep_alive` tuning, `doctor` latency check |
| JetStream growth | Low | Stream `max_age`/`max_msgs`, KV `history=10` |
| Orchestrator single point | Low | Stateless-ish: state in KV, restart rebuilds; horizontal scaling later if needed |

## 16. Assumptions

- "Ormica-style" = durable state snapshots + guarded transitions + append-only event log + **stigmergic coordination** (state/intent/progress posted to a shared field others sense). Concrete references reviewed: ormica (pypi), SwarmHarness arXiv:2605.28764, Cotal on NATS, Synadia Agent Protocol, Internet-of-Agents 2407.07061, aden-hive/hive. §0 distills them into decisions D1–D6.
- Inference endpoints are external to this repo (host-native Ollama using ROCm, LAN nodes, or OpenAI-compatible); no GPU passthrough is configured here.
- Full shell is intentionally unrestricted inside the worker container.
- Default models are placeholders; real model names are set per node via env.

## 17. Definition of done

- `docker compose up` brings NATS + orchestrator + three workers healthy with an external inference endpoint.
- `meshctl submit` on a small coding task produces a plan, edited files in the workspace, a critic verdict, and a terminal state visible via `meshctl tail`.
- Orchestrator restart mid-task resumes without duplicate side effects; worker crash redelivers without re-running completed tools.
- `pytest` green; `docs/architecture.md` and `docs/protocol.md` match the implementation; `scripts/smoke.sh` runs end-to-end.

## 18. Role division: Subagent vs. Parent (Orchestrierung)

Bei der Umsetzung (M7, 2026-11-01) wird die Arbeit zwischen einem abgegebenen Hermes-Subagenten und dem Parent-Assistenten geteilt; das Task-Board (`todo_list`, Kategorien SUBAGENT / ICH / BLOCKIERT) hält den laufenden Zustand fest.

- **Subagent** (delegiert via `delegate_task`, lokales Modell `edtorre/gemma4-26b-a4b-it-qat-agent`, 25.2B mit Tools): gut abgegrenzte, mechanische Teilaufgaben gegen verifizierte APIs. Beispiel hier: `tests/test_integration_adapter.py` schreiben.
- **Parent (ICH)**: alles was auf Nutzer-Kontext, Verifikation, Credentials oder koordinierende Entscheidungen aufbaut — Adapter/Unit-Tests selbst schreiben, Subagent-Ergebnisse auf der Platte gegenprüfen, NATS-Live-Lauf (braucht dev-Token, der nicht an Subagenten weitergegeben wird), Commit.
- **Blockiert**: Live-NATS-Test — Testinstanz auf 4223 läuft nicht; dev auf 4222 verlangt dev-Token-Auth (nicht plaintext, nicht an Subagenten).

Regel: Subagent-Selbstberichte sind nie Fakten — jede behauptete Datei/Ausgabe wird vom Parent selbst verifiziert, bevor eine Aufgabe als completed markiert wird. Details im Skill `subagent-orchestration`.

### Was der Subagent übernehmen soll (Zuordnung je Phase, Stand 2026-11-01)

Für die aus den Referenzen abgeleiteten offenen Stücke (§0.5 M7-CLR/M8/M9/M10) gilt dieselbe Rollentrennung. Der Subagent macht die **mechanischen, klar abgegrenzten Implementierungsteile gegen verifizierte APIs**; der Parent behält Credentials, Verifikation, NATS-Live-Lauf und Commit.

- **M7-CLR (meshctl registry-Befehl + Test):** → **Subagent**. Reine CLI-Erweiterung nach dem bestehenden `cmd_tasks`-Muster (Bus intern instanziieren, `list_capabilities()`, finally `close()`), Unit-Test ohne NATS via monkeypatch auf `memnc.cli.Bus`. Kein Credential, kein Live-NATS nötig → ideal delegierbar. **Parent**: Dateien auf Platte verifizieren, `py_compile` + pytest selbst laufen lassen, Commit.
- **M8 Trust-Ledger + Attribution + Multi-Router:** teilen. **Subagent**: `mesh.trust`-KV-Methoden (credit accrue/decay, Attributions-Eintrag) + Router-Scorer als pure Funktion über presence+trust (NATS-freie Unit-Tests). **Parent**: NATS-Live-Run (Token), Routing-Integration in `meshctl`-Roster, Commit.
- **M9 Subject-Identity + ACLs + Capability-Gate:** teilen. **Subagent**: definierte Teile der JWT-/ACL-Konfiguration und Gate-Prüflogik (allow/deny reine Funktion + Unit-Tests). **Parent**: Token-Handling/NATS-wire-Konfiguration (Credentials), Orchestrator-Grant-Autorisierung konzipieren, Live-Verifikation, Commit.
- **M10 Queryable Ledger + Fan-out:** teilen. **Subagent**: `mesh.task.submit`-Fan-out-Methode + Ledger-Read-Helper gegen verifizierte Bus-APIs + Unit-Tests. **Parent**: Ledger-Sicht in `meshctl`-Integration, Orchestrator-seitige Subtask-Decomposition als Design-Entscheidung, Live-NATS-Test, Commit.

Grenze: Subagent bekommt **nie** den NATS dev-Token (nicht plaintext, nicht an Kinder); alle Live-NATS-Anschlusstests macht der Parent, sobald eine Testinstanz verfügbar ist (4223 oder 4222 mit Token).
