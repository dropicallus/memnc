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

### 14.1 Roadmap: symmetric 2-model self-orchestration (2026-11-02)

Goal: both nodes are **architecturally identical** — every node can submit, plan, claim, code, critique, and fan out. No fixed role, no "controller node". The orchestrator is gradually demoted from dispatcher to optional referee (M11 direction, completed in M14).

| Step | Content | Done criterion |
|---|---|---|
| **M12 — M11 live, both nodes** | Rebuild Node A with M11 code; bring Node B (maco) up to the current state (`docker-compose.node.yml`); `mesh_claim=true` on Node-B workers; live test: `meshctl submit --claimable` → Node-B worker claims via CAS and runs the pipeline | Task submitted on A, claimed+DONE by B; second run in reverse (B submits → A claims) | **[DONE 2026-11-02]** |
| **M13 — Agent as live submitter** | Coder/planner agent calls `mesh_submit` from inside a running task; the subtask lands in the mesh and is claimed by the *other* node | Proof: task on A creates a subtask, B works it off (and vice versa); `meshctl ledger` shows both nodes as submitter+claimer | **[DONE 2026-11-02]** |
| **M14 — Orchestrator-less dispatch** | Claimable tasks get `next_role`; only the matching role can claim (CAS role gate); the claiming worker continues the pipeline **itself** (reuses FSM `transition`) and makes the next step claimable (RECEIVED→PLANNING→CODING→CRITIQUE→DONE). Orchestrator remains a pure spectator; `_dispatch_claimed` optional | Submit with the orchestrator stopped: task is still claimed and executed; orchestrator restart only aggregates | **[CODE DONE 2026-11-02, commit b49e142: next_role field, role-gated CAS claim, worker starts the pipeline itself (reuses FSM `transition`), orchestrator skips worker-driven tasks; 124 unit tests green. LIVE TEST STILL OPEN: deploy images, claimable submission, verify the pipeline without orchestrator dispatch]** |
| **M15 — Symmetric topology** | Node B runs its own orchestrator (responsible only for its own submissions/fan-out); one broker (NATS A) or leaf nodes; both nodes config-identical except models | Both nodes submit fan-out tasks simultaneously without collision (CAS/KV protects); docs §3 updated | **[CODE DONE 2026-11-02/03, commits 86fc593 + 928e2ed: owner-routed submit subjects, origin_node, ownership guards, worker-side fanout split, Node-B orchestrator service, wildcard submit subscription + per-node durable names + legacy node-a acceptance; LIVE TEST STILL OPEN: simultaneous fan-out run of both nodes]** |
| **M16 — Demo: real peer orchestration** | A task is placed on B, B plans, fan-out lands symmetrically on A+B, both models (different Ollamas) work each other's shares, cross-node critique | End-to-end run with alternating direction ×2 (A→B and B→A shares), `task.done` with bidirectional attribution in the ledger |

Non-blockers (optional afterwards): broker JWT/ACL hardening (M9 remainder), raise `MAX_TOOL_STEPS` for the 3B coder, trust utility in the claim decision (M8 score at claim poll).

### 14.2 Expansion stage: smoke gate, UI, human steering (2026-11-03)

*Status: S1 is implemented and green (`39be633`). S2–S4 are pure planning — nothing built yet. The outlook sections 14.3/14.4 are also vision, not a roadmap with dates.*

This stage comes **after** M16 (real peer orchestration live) and turns the mesh into a daily-usable tool. Order is deliberate: first the gate (S1), then the models (S2), then visibility (S3), then control (S4).

| Step | Content | Done criterion |
|---|---|---|
| **S1 — Smoke gate** | Make `scripts/smoke.sh` hard: submit → poll `state` until DONE/FAILED with timeout (instead of a blind `tail --limit`); assert on end state DONE, exit code 0/1 (CI-capable); modes `--claimable` (referee mode: orchestrator accepts but does not distribute — workers with `MESH_CLAIM=true` claim on their own = M14 proof) and `--fanout`; runnable against both the Ollama AND the OpenAI backend (`INFERENCE_PROVIDER` parameter) | `./scripts/smoke.sh` ends green on maco and on .180; red exit when the task FAILED/times out; smoke reused for the M14/M15/M16 live tests |

**S1 result (2026-11-04):** green on maco (pipeline PLANNING→CODING→CRITIQUE→DONE in 39 s, commit `39be633`). Observations: mannix tool-schema jitter (4/7 calls with wrong argument names), critic laxity (`hello from memnc` instead of `hello from the mesh` passed) — both S2/S4-relevant. On .180 the gate correctly went red: llama3.2:3b attempted 6/7 tool calls with wrong argument nesting (`p:` instead of `pattern:`, `f:` instead of `path:`) and wrote a 6-byte-truncated hello.py → FAILED/exit 1 (gate behavior correct); qwen2.5-coder:7b failed as planner ("no JSON object found"), while a direct Ollama probe with a minimal prompt immediately produced perfect JSON → the problem is the long planner prompt/context situation on the CPU box, not the model in general. **M14 `--claimable` live: pending** — submitting works (task lands RECEIVED/claimable), but no claim happens because `MESH_CLAIM=false` on all workers of both nodes; next step: `MESH_CLAIM=true` on the .180 worker compose, repeat the smoke test with `--claimable`, document the claim transitions (claimed_by/node-b) as evidence.
| **S2 — Swift as default backend** | Worker compose to `INFERENCE_PROVIDER=openai`, `INFERENCE_BASE_URL=http://host.docker.internal:8097/v1`, `MODEL_*` to Swift-1.5 (Qwen3.8-27B GSQ, llama-server :8097); mannix 27b gets offloaded from the 6800XT (VRAM: Swift ~13 GB + mannix 16 GB do not fit together); mannix stays the reserve model for discussion rounds | Smoke (S1) green with Swift; `meshctl doctor` shows Swift latency; no thinking leaks (Swift thinks briefly, the num_ctx budget holds) |
| **S3 — UI (mesh console)** | Local web console (small HTTP server, no framework obligation): task list + state colors, ledger view (registry/grants/trust), live `tail` (SSE or polling), node roster with load/latency; read-only — writes nothing | `http://localhost:<port>` shows live the same state as `meshctl ledger`/`tasks`; works from both nodes (Node-B renders against its own broker) | — **IMPLEMENTED 2026-11-04:** `src/memnc/console.py` (stdlib `ThreadingHTTPServer`, polling frontend 3 s, tasks/caps/grants/trust/ledger, task-detail snapshot via `/api/task/<id>`; `python -m memnc.console --port 8790`)
| **S4 — Human-in-the-loop (steering)** | Approval gate in the critique step: task parks in CRITIQUE with the `needs_human=true` flag (KV), console/Telegram shows diff+verdict, human approves/rejects → FSM continues; Telegram bot as the second interface (submit, approve, tail); grants remain the authorization layer (M9) | End-to-end: task with a criticism-worthy patch parks, human rejects → revision loop, approves → DONE; everything operable from Telegram too |

Scope note: S4 replaces neither the M9 ACLs (machine layer) nor the trust scores (statistical layer) — the human veto is the third layer on top.

### 14.3 Outlook: self-organization & social competence (2026-11-03)

Two core elements carry the mesh concept beyond DeepSeed's dsh-superagent (whose panel shows only "what needs a person" — the human in the loop; our agents additionally organize *among themselves*):

**Self-organization** — the mesh distributes itself, no central dispatcher:
- *already real:* cross-node CAS claiming (M11/M12), agent subtasks across hierarchy levels (M13), owner routing + fan-out (M15)
- *expansion:* **dynamic roles** — the discussion (disc_20261103_230643) concluded that fixed leader-follower roles create deadlocks under async latency; state-based coordination + capability hierarchy instead. **Trust score in the claim decision** (M8 score at claim poll): reliable nodes automatically inherit more attractive tasks — reputation-driven self-organization.

**Social competence** — negotiation instead of command execution:
- *model↔model:* peer discussion with substantive objection and self-termination ([DONE], disc_20261103_230643) as a demonstrable negotiation pattern; critique step as institutionalized peer review; registry/capabilities as a social protocol ("who can do what")
- *model↔human:* trust ledger as social memory, grants as the authority instance (M9), approval gate (S4) as human veto
- *expansion:* task-driven consensus finding — critical tasks may trigger a model debate before execution (the discussion harness becomes a mesh primitive); verdict recommendations flow into the trust score.

**Design consequence for S3:** the console is the observation window of self-organization, not a dashboard: it shows *who organizes how* (claims, roles, node roster) and *where the negotiation stands* (critique verdicts, trust, grants) — not just a task list.

### 14.4 Outlook: task affinity, evidence metrics & learned distribution (2026-11-04)

**Task affinity / "don't take the task away"** (from the DeepSeek dsh-superagent prototype, S., 2026-11-03):
- Once a task is distributed to a model, it should not be immediately taken away again — models tend to ("I can do this better"), but redistribution is inefficient: the receive context (prefill, history, workspace state) must be rebuilt. DeepSeek solves this via a **long-term KV cache**; our counterpart: task stability at the workflow level.
- *Status:* partially in — `origin_node` routing (M15) binds tasks to the owner node, claims are hard (CAS), critique loops stay with the same worker. **Not in:** affinity at the model level. Once cloud models enter the mesh (S2 variant with an external API), cache hits become a first-order cost/latency factor: redistribution = full prefill = full token payment; staying on the model instance = cache hit.
- *Expansion:* routing considers `(node, model_id)` affinity — resumed tasks/follow-ups prefer the instance that already holds the context in cache. Hot-standby instances per role (Swift/Ollama) as cache carriers.

**Evidence metrics for self-organization** (idea F., 2026-11-04; reference Graphify-Labs/graphify):
- Self-organization is unsteerable without measurement. We need an efficiency benchmark: **naive tool-call loop vs. structured search** (graph/retrieval-assisted) on identical tasks — measured by: tokens/task, **failed-tool-call ratio** (in the S1 run: 4/7 mannix calls faulty), time-to-DONE, critique round trips.
- Adopt the Graphify benchmark pattern: fixed agent, fixed questions on a real codebase (e.g. memnc itself; the ~1M-LOC ERPNext as benchmark reference: grep baseline 70.8% vs. graph 82.0% key-fact coverage), exact graders instead of an LLM judge. Independent replications warn: the structure advantage concentrates on multi-hop queries; grep wins simple lookups — so the mesh metric must measure **per task type**, not as a single number.
- Feeds a mesh primitive: task context (plan, workspace state, relevant code graph) as a queried resource instead of a re-read context.

**Self-learning distribution (ant-based):**
- Currently: static claim polling + owner routing. Later: distribution as stigmergy — tasks leave pheromone-like traces (success/cost signals per `(node, model, task_type)`), trust score + affinity are continuously updated from them instead of configured. Ant algorithm as the scoring scheme for the claim race; the already-measured evidence metrics are the starting signal function.

**Role fine-tuning (LoRA):**
- Fine-tune models with LoRA on social interaction and role behavior (planner/coder/critic as behavior styles, not just prompt roles) — fits the memory vision (Growing-LoRA, Fisher-Merge): the mesh roles become trainable model states; the critic learns from S1-like runs when strictness is justified (see critic laxity in the first S1 run).

### 14.5 Outlook: AutoSwarm — heterogeneous agent harnesses under one roof (2026-11-04)

Reference: [HKUDS/AutoSwarm](https://github.com/HKUDS/AutoSwarm) (arXiv 2610.33048, MIT) — not a replacement for our mesh design, but several concepts well suited for further expansion:

- **Capability-based agent selection instead of fixed roles:** AutoSwarm selects agents by capability profile (coding, research, browsing, ...), not by configuration role. Conceivable for memnc: the roster holds not only `(node, model)` but capability tags; the router matches task requirements against capabilities instead of static roles.
- **Shared context/memory across harness boundaries:** AutoSwarm's core thesis is "Unify your context" — agents have isolated contexts; the coordination layer shares context/memory. Our counterpart: the NATS ledger as a context bus. Outlook: task context (plan, workspace state, relevant graph excerpts) not as one big prompt but as a queried resource (connects to the §14.4 evidence metrics).
- **Collaboration formation with acceptance criteria:** before distribution, responsibility boundaries and explicit acceptance criteria are defined per subtask. Maps directly onto our critique loop: make the DONE definition machine-checkable instead of a benevolent critic.
- **Execution coordination as shared state:** dependencies, supervisory relationships and review feedback update a common state instead of message ping-pong — our ledger is already built this way; extend it into explicit dependency edges between subtasks.
- **Adapter idea:** AutoSwarm orchestrates foreign harnesses (Codex, Claude Code, Hermes, ...) via their native CLIs. Our mesh is currently homogeneous (own harness on both nodes). Later conceivable: dock foreign harnesses as worker adapters — for that, the ledger/claim API must suffice as an adapter interface.

Status: all outlook, none of it implemented.

### 14.6 Expansion stage: Telegram channel + console expansion — visibility, intervention, human-in-the-loop (2026-11-05)

*Trigger:* the M14 live test succeeded (task `1a1ad38d` → DONE rev=0 without orchestrator; the Gemma-31B critic verified itself via the MCP shell). Self-organization works — but it is invisible: you have to poll `meshctl state` yourself and grep through log files. The tests were laborious; the mesh needs a window through which Frank can **follow along, intervene, and steer**. Models: grok.bot / dots — a bot interface as the primary interface to the mesh.

*Relationship to S3/S4 (§14.2):* S3 (mesh console, read-only, port 8790) is implemented. This stage concretizes S4 and builds on it — same order: first see, then intervene, then steer. No new framework, small steps, each individually testable.

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

*Status (2026-11-09):* T0, T1, T6, T7.5 and T2 shipped. T2 (commit `9e463a0` + `fae6b2c` + `46db138`): `ControlCommand` on subject `mesh.control`; orchestrator grant-checks each command (deny-by-default via `acl.check_registry_write`, CHAIN level) and records accepted actions as `human.submit` / `human.cancel` / `human.retry` events on the task snapshot. Telegram commands `/submit`, `/cancel`, `/retry`, `/stop worker <node>` forward via request-reply; response is JSON (`{"ok":true,"task_id":...}` or `{"error":...}`). The Telegram agent id is `telegram-<chat>` (KV keys reject `:`); a `CHAIN` grant for it must be issued once via `meshctl grant`. `stop_worker` withdraws all capability records on the named node — the process itself keeps running until the operator stops it on the host (per scope note above). Submit path is shared with meshctl submit (`_accept_submission`, incl. the M11 depth check). T3–T5, T7, T8 open.

### 14.7 Outlook: model demand planning & LAN model sharing (2026-11-05)

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


## 14.8 Shipment: distribution & deployment

Two artifacts, two audiences (decided 2026-11-09):

- **Mesh services — docker compose (status quo, stays).** One `docker compose up -d` on a fresh host stands up NATS + orchestrator + workers + telegram observer. Workers need host-native Ollama anyway (ROCm), so containers never carry inference. Image is `memnc:latest`, built from the same `pyproject.toml`; the only node-specific input is env (`.env`: token, allow-list, VRAM/GPU, NATS URL). No registry dependency for now — `compose build` on the target node is the delivery.
- **`meshctl` — installable package (uv/pip).** Client-side only: hosts and agents (Hermes, LAN admins) must not run Docker to talk to the mesh. The package already exists (`pyproject.toml`, Python 3.11+); ship it as `uv tool install memnc` (or `uv sync` from a clone). Client needs no token for a plaintext NATS; with auth, `NATS_URL` carries credentials via query params (`nats://user:token@host:4222`) — document that.

Explicitly NOT shipped as a uv package: orchestrator/worker/observer runtimes. They are stateful-in-practice (Ollama co-location, GPU env, container isolation for the shell sandbox — see §15) and packaging them for pip would duplicate the compose path for no gain. If a later phase wants bare-metal workers, that is a separate `memnc-worker` package decision, not now.

Checklist for shipping a new node:
1. `git clone` + `docker compose up -d --build` (or pull prebuilt image later).
2. Node env: `NODE_NAME`, `NODE_ROLES`, `NODE_VRAM_GB`, `NODE_GPU`, `OLLAMA_BASE_URL`, `TELEGRAM_*` only on the observer host.
3. Install meshctl on the operator/agent host: `uv tool install` from the repo.
4. `meshctl doctor` → `meshctl roster` to verify presence.

## 14.9 Hermes plugin as second approval surface (PLANNED, not built)

Plan only — deliberately not implemented yet (Frank, 2026-11-09).

A small Hermes-side plugin/skill wrapping `meshctl` as a second approval
channel next to Telegram (§14.7/T7.5). Both channels are first-come
first-served on the same KV record + `mesh.model.decision` stream; the
second one sees "already decided". Scope when built:

- skill with the `mreqs` / `decide approve|reject` workflow (meshctl from
  the host, `NATS_URL=nats://localhost:4222` override, PYTHONPATH caveat);
- optionally thin shell scripts so agents call `meshctl-mreqs` /
  `meshctl-decide` instead of remembering env flags;
- the Telegram observer keeps its minimal approve/reject write path
  unchanged; the observer stays read-only apart from that.

Status: a draft skill (`t7-gate`) exists in this Hermes profile; treat it
as the reference for the eventual in-repo plugin. Nothing of this ships
with memnc until Frank green-lights it.

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

## 18. Role division: subagent vs. parent (orchestration)

During implementation (M7, 2026-11-01) work is split between a delegated Hermes subagent and the parent assistant; the task board (`todo_list`, categories SUBAGENT / ME / BLOCKED) keeps the running state.

- **Subagent** (delegated via `delegate_task`, local model `edtorre/gemma4-26b-a4b-it-qat-agent`, 25.2B with tools): well-scoped, mechanical subtasks against verified APIs. Example here: writing `tests/test_integration_adapter.py`.
- **Parent (ME):** everything that builds on user context, verification, credentials or coordinating decisions — writing the adapter/unit tests itself, cross-checking subagent results on disk, the NATS live run (needs the dev token, which is not shared with subagents), the commit.
- **Blocked:** live NATS test — the test instance on 4223 is not running; dev on 4222 requires dev-token auth (not plaintext, not given to subagents).

Rule: subagent self-reports are never facts — every claimed file/output is verified by the parent itself before a task is marked completed. Details in the `subagent-orchestration` skill.

### What the subagent should take over (assignment per phase, as of 2026-11-01)

The same role split applies to the open pieces derived from the references (§0.5 M7-CLR/M8/M9/M10). The subagent does the **mechanical, clearly scoped implementation parts against verified APIs**; the parent keeps credentials, verification, the NATS live run and the commit.

- **M7-CLR (meshctl registry command + test):** → **subagent**. Pure CLI extension following the existing `cmd_tasks` pattern (instantiate Bus internally, `list_capabilities()`, finally `close()`), unit test without NATS via monkeypatch on `memnc.cli.Bus`. No credential, no live NATS needed → ideally delegable. **Parent:** verify the files on disk, run `py_compile` + pytest itself, commit.
- **M8 trust ledger + attribution + multi-router:** shared. **Subagent:** `mesh.trust` KV methods (credit accrue/decay, attribution entry) + router scorer as a pure function over presence+trust (NATS-free unit tests). **Parent:** the NATS live run (token), routing integration into the `meshctl` roster, commit.
- **M9 subject identity + ACLs + capability gate:** shared. **Subagent:** defined parts of the JWT/ACL configuration and the gate check logic (allow/deny pure function + unit tests). **Parent:** token handling/NATS wire configuration (credentials), design of the orchestrator grant authorization, live verification, commit.
- **M10 queryable ledger + fan-out:** shared. **Subagent:** `mesh.task.submit` fan-out method + ledger read helper against verified bus APIs + unit tests. **Parent:** ledger view in the `meshctl` integration, orchestrator-side subtask decomposition as a design decision, live NATS test, commit.

Boundary: the subagent **never** gets the NATS dev token (not plaintext, not given to children); the parent performs all live NATS connection tests as soon as a test instance is available (4223, or 4222 with token).

