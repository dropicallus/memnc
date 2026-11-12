# MEMNC — Distributed Multi-Agent Mesh over NATS



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

§0.5) | An external harness (Hermes/pi, then Claude Code) joins the mesh via `memnc.agent`, advertises skills in `mesh.registry` KV, receives anycast work, replies; **adapter+registry done (commit 374fa3e); meshctl registry view is the remaining M7-CLR clearance** |
| M8 | Utility routing + presence + trust (§0.5) | Router scores `U=w1·cap+w2·(1−load)+w3·(1−lat)+w4·trust` over presence KV (TTL); `meshctl` shows live roster + load; **trust ledger, submitter-signed attribution, >1 concurrent router work (SwarmHarness)**; multi-machine topology documented in §3; **trust ledger + utility scorer done (commit 03f1f35); live router on NATS + submitter-signed attribution is the remaining M8 clearance** |
| M9 | Subject identity + ACLs + capability gate (§0.5) | NATS token → per-agent JWT/users ACLs; sender token policed on the wire; pinned per-agent durable inbox; **registry write authorized via orchestrator grant, deny-by-default (ormica permission-chain)**; documented in §5.2 + architecture.md; **grant ledger + gate (acl.py, deny-by-default) + meshctl grant/grants/revoke done (M9); LIVE GATED 2-NODE RUN PASSED : node B ([REDACTED], node-b) deployed via docker-compose.node.yml against node A NATS, heartbeat-bootstrap grant + registry verified, meshctl submit completed end-to-end across node-a/node-b planner/coder/critic (task.done); broker-side JWT/user-ACL config remains optional hardening** |
| M10 | Queryable mesh ledger + fan-out tool-call (§0.5) | `meshctl registry` + presence/state readable as a ledger any agent queries; orchestrator emits subtasks as `mesh.task.submit` fan-out and returns immediately, anycast workers claim them (hive run_worker shape); **DONE : `mesh.ledger` request/reply + `meshctl ledger` (orchestrator answers, Bus fallback) + `submit --fanout` decomposition (≥2 plan steps → independent subtasks, parent parks in CODING, `_check_fanouts` aggregates DONE/FAILED); live: 2 subtasks emitted on submit, 1 DONE / 1 failed (worker tool-step limit, not M10), aggregation correct; broker-side JWT/user-ACL remains optional hardening** |

Each milestone is independently demoable; M2 is the checkpoint that proves the mesh before any model is involved.

#### 15. Risks & mitigations

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

During implementation (M7, ) work is split between a delegated Hermes subagent and the parent assistant; the task board (`todo_list`, categories SUBAGENT / ME / BLOCKED) keeps the running state.

- **Subagent** (delegated via `delegate_task`, local model `edtorre/gemma4-26b-a4b-it-qat-agent`, 25.2B with tools): well-scoped, mechanical subtasks against verified APIs. Example here: writing `tests/test_integration_adapter.py`.
- **Parent (ME):** everything that builds on user context, verification, credentials or coordinating decisions — writing the adapter/unit tests itself, cross-checking subagent results on disk, the NATS live run (needs the dev token, which is not shared with subagents), the commit.
- **Blocked:** live NATS test — the test instance on 4223 is not running; dev on 4222 requires dev-token auth (not plaintext, not given to subagents).

Rule: subagent self-reports are never facts — every claimed file/output is verified by the parent itself before a task is marked completed. Details in the `subagent-orchestration` skill.

### What the subagent should take over (assignment per phase, as of )

The same role split applies to the open pieces derived from the references (§0.5 M7-CLR/M8/M9/M10). The subagent does the **mechanical, clearly scoped implementation parts against verified APIs**; the parent keeps credentials, verification, the NATS live run and the commit.

- **M7-CLR (meshctl registry command + test):** → **subagent**. Pure CLI extension following the existing `cmd_tasks` pattern (instantiate Bus internally, `list_capabilities()`, finally `close()`), unit test without NATS via monkeypatch on `memnc.cli.Bus`. No credential, no live NATS needed → ideally delegable. **Parent:** verify the files on disk, run `py_compile` + pytest itself, commit.
- **M8 trust ledger + attribution + multi-router:** shared. **Subagent:** `mesh.trust` KV methods (credit accrue/decay, attribution entry) + router scorer as a pure function over presence+trust (NATS-free unit tests). **Parent:** the NATS live run (token), routing integration into the `meshctl` roster, commit.
- **M9 subject identity + ACLs + capability gate:** shared. **Subagent:** defined parts of the JWT/ACL configuration and the gate check logic (allow/deny pure function + unit tests). **Parent:** token handling/NATS wire configuration (credentials), design of the orchestrator grant authorization, live verification, commit.
- **M10 queryable ledger + fan-out:** shared. **Subagent:** `mesh.task.submit` fan-out method + ledger read helper against verified bus APIs + unit tests. **Parent:** ledger view in the `meshctl` integration, orchestrator-side subtask decomposition as a design decision, live NATS test, commit.

Boundary: the subagent **never** gets the NATS dev token (not plaintext, not given to children); the parent performs all live NATS connection tests as soon as a test instance is available (4223, or 4222 with token).


## 19. Research-informed orchestration priorities (26-paper corpus, )

Source: `~/research-papers/analysis_priors_proposal.md`; full synthesis with LoRA/memory priorities in `~/research-papers/synthesis.md` §5.

- **P0 — Leader-Worker orchestration (MLPO, 2507):** a leader policy synthesizes/corrects worker outputs and routes subtasks to maximize global reward. Maps onto the orchestrator grant/authorization design (§18) and the T7 trust ledger: trust scores weight the leader's per-worker reward credit.
- **P0/Mesh+Memory crossover — Role-specific LoRA adapters (MoRSE, 2608_09251):** distinct weight-level adapters for Planner/Coder/Critic roles instead of one shared adapter; subspace-divergence check before merging adapters (`memnc/workers/` + Fisher-Merge pipeline).
- **P1 — Latent communication (preprints_202609_1298):** exchange low-dimensional hidden states/KV-cache summaries between agents instead of raw text; NATS-transportable, token-cheap.
- **P1 — Neutral consensus prompts (04_majority_following):** trust-weighted voting in the ledger (`w_i ∝ β_i`); neutral, shuffled option labels when collecting worker votes; 2–3 debate rounds suffice for capable participants.
- **P2 — Incentive governance (2605_25815):** complexity-weighted evaluation metrics (logical depth over file counts) to prevent score inflation in self-evolving worker populations (`memnc/evaluation/`).

### 19.2 New insights 2026-11-11/12 (research + competition check)

- **P1 — DEALS (2609.33768):** decentralized routing without LLM calls —
  direct prior art for M8's utility scorer: score candidates from
  presence/trust metadata only, no model call in the routing path.
  Validation: our scorer (03f1f35) already follows this shape; keep it
  LLM-free permanently.
- **P1 — ToolSched (2611.05269):** resource-aware shell scheduling for
  shared local hosts (+148% resource utilization) — informs T6 needs-
  constraints (roster advertises RAM/VRAM/load) and M11c resource verbs.
- **P2 — Single-LoRA dominance (2603.01097):** one consolidated LoRA beats
  a portfolio of specialized LoRAs — counter-pressure on B2/B4: merge role
  adapters aggressively (Fisher-Merge), do not accumulate adapters per
  role long-term.
- **P2 — MoRAM/CoDyRA line:** Growing-LoRA as parametric memory (large
  rank ~ data volume, untrained capacity = overfitting risk, measured via
  train/valid divergence) — design-first track in roadmap P2b, replaces
  RAG for the memory vision.

### 19.1 Build order: research → mesh integration (triage )

Triage of the 26-paper corpus against the current roadmap. Ordering rule: the
experience-graph steps 1–5 (## 20. Mesh security: secure-by-design (AAOS SDV principles)

Source: Android Developers Blog, "AAOS SDV – Secure by Design" (,
android-developers.googleblog.com). Reference: issue #2 deferred items (H4, M7,
M11, L14). Mapping principle-by-principle; build order at the end.

### 20.1 Principle mapping

| AAOS SDV principle | memnc status | Gap → item |
|---|---|---|
| **Isolation is default, sharing explicit** (VM per domain) | DONE: every mesh service runs its own container (read-only rootfs, `cap_drop: ALL`, `no-new-privileges`, host-matched user, workspace-only bind mount, no docker socket) — already hardened in compose | MCP shell tools stay inside the same container; acceptable, shell env is curated (39ca961) |
| **Deny-by-default process/communication permissions** (UID + SELinux) | HALF: grant gate for control commands (M7/#2), pull whitelist `[]` default, curated subprocess env | The same posture is missing at the NATS transport: one shared token, identity is *claimed*, not *proven* → **H4** |
| **Layered access control** | Grant ledger (ROOT/CHAIN, deny-by-default) gates high-level actions | Transport-level subject scoping per node; **M7 revocation tombstone** so a withdrawn node is cut immediately, not at roster TTL |
| **Integrity: secure software delivery** (signed images, verified boot) | Pull path human-gated + whitelisted (T7); CI on push/PR | Base images pinned by tag, not digest; e2e artifact check is text-based → **M11**: SHA256 artifact pinning + digest-pinned base images; T8 LAN GGUF sharing must add checksum-before-load |
| **Vulnerability management lifecycle** (scanning, bulletins, triage) | CI runs ruff + pytest (L14 done) | **L12**: `pip-audit` + `trivy image scan` in CI, Dependabot for the few deps, findings get severity + tracking issue; monthly security review slot (like the per-paper cron) |
| **Memory-safe development** (Rust) | Python | Honest non-goal: rewriting in Python-land pays nothing at 3 nodes. The Python equivalents of the memory-safety bug class are exactly the 39ca961 invariants: bounded reads, killed process groups, no env inheritance. Optional: mypy strict in CI |

### 20.2 H4 design: transport-level identity binding (P0)

Goal: "who am I" becomes a transport property, not a payload claim.

- **Server**: replace `--auth <token>` with a mounted `nats.conf` defining one
  account `MESH` with per-node **users**, each with its own NKey seed and
  explicit allow-lists. Permissions follow the subject map in `schema.py`:
  - orchestrator user: full `mesh.>` control (submit, grants, control, DLQ)
  - worker user `<node>`: sub `mesh.agent.<role>.cmd`, `mesh.task.>`,
    `mem.agent.<node>.work`; pub `mesh.agent.<role>.result`,
    `mesh.node.<node>.>`, `mesh.bid.>`; KV/JS via account-scoped exports
  - deny-by-default: no user gets `mesh.>` wholesale
- **Client**: `bus.connect()` uses `user_credentials=` (nsc `.creds` file)
  mounted per container; shared `NATS_TOKEN` is removed from compose once all
  nodes are migrated (flag `MESH_AUTH_MODE=creds|token` for the transition).
- **Key storage**: per-node seed files live in gitignored `secrets/` (one
  `<node>.creds` per node); generation via `nsc` or `nkeys` at deploy time,
  never committed. Loss = re-generate + redeploy that node.
- **Verification**: with creds, a spoofed envelope claiming node `<x>` from
  another connection is impossible at the transport; the capability gate then
  only needs to authorize *actions*, not *identities*.

### 20.3 M7 design: revocation tombstone (P0, small)

- KV bucket `mesh_revocation` (no dots), key `revoked.<agent_id>`, value =
  reason + timestamp, written by orchestrator on `stop_worker` / grant withdraw.
- `acl.check_*` and the capability gate read the tombstone first: a revoked
  agent is refused on the next message, not at the next roster TTL expiry.
- Un-revoke is a human action (delete the key via meshctl, never via Telegram).

### 20.4 Build order

1. **P0**: H4 creds migration (§20.2) — biggest single win; do as one commit
   with a transition flag so running nodes keep working until they sync.
2. **P0**: M7 tombstone (§20.3) — small, lands on the existing gate.
3. **P1**: M11 slice — SHA256 artifact verification in e2e + digest-pinned
   base images in compose; `pip-audit` + trivy in CI (L12 remainder).
4. **P2**: T8 checksum-before-load when the offline node returns; mypy strict
   as a stretch goal.

Non-goals / standing rules: approvals stay human (no new authority paths);
`.env` stays gitignored; no default creds shipped in compose (fail-closed, like
`NATS_TOKEN:?`); every new deny-path gets a test before merge.

## 21. Security concept (part 2): revocation, remaining hardening, and the autonomy line

(per-node NKeys, `df1951e`) landed. This chapter bounds the remaining
security work so the mesh stays what it is: a loose federation of
autonomous, OpenClaw-flavoured agents — not a centrally controlled fleet.

### 21.1 The autonomy line (OpenClaw genes, non-negotiable)

Hardening must not cross this line:

1. **No central kill switch.** meshctl can *refuse to admit* a revoked
   node (admission control), but there is no "freeze all nodes" command.
   A node that loses the bus degrades to local work and rejoins on its
   own when connectivity returns. Revocation is about trust in the
   *mesh identity*, never about stopping the agent process itself.
2. **Terminal-capable nodes stay terminal-capable.** Some nodes take
   tasks that run on a real shell (builds, ops, local tooling).
   Sandboxing is therefore **opt-in per node, like Hermes**: default is
   no sandbox, and a node opts into a container sandbox for specific
   task classes when *its operator* decides so. The orchestrator may
   *prefer* sandboxed nodes for untrusted tasks (auction tie-breaker)
   but may never *require* a sandbox for the mesh to function.
3. **Joinability stays cheap.** Plain NATS + one seed file per node
   (`gen_nkeys.py`) is the whole onboarding. No CA, no certificates, no
   control-plane sign-off workflow. If a hardening step makes joining
   harder than copying one file, it fails this line.
4. **App-level gates stay where they are.** MESH_GATE, grant KV,
   MESH-empty pull whitelist are *policy* layers an operator configures;
   the transport layer (NKeys) is *identity*. The concept never merges
   the two: identity says "who", policy says "what".

### 21.2 Trust tiers (the model behind the knobs)

| Tier | Who | Transport | Sandbox | Notes |
|------|-----|-----------|---------|-------|
| 0 | Orchestrator | NKey, `mesh.>` | none (trusted host) | holds control plane |
| 1 | Household nodes | NKey, worker scopes | none by default | full terminal access, local trust |
| 2 | Guest/experimental nodes | NKey, worker scopes | opt-in docker sandbox | operator choice per node |

Tiers are configuration, not code: a node declares its tier in its own
`.env`/config; the orchestrator reads it from roster NodeInfo and uses
it only for *scheduling preference* (e.g. prefer tier >= 2 for tasks
marked `untrusted: true`). No tier ever blocks basic mesh operation.

### 21.3 M7 — revocation tombstone (concept)

- **Store:** KV bucket `mesh_revocations` (subject-scoped to
  `$KV.mesh_revocations.>` for all users; orchestrator is the only
  writer). Key: the revoked NKey public key (56 chars, `U`-prefixed).
  Value: JSON `{revoked_at, reason, by}`; TTL optional — tombstones
  expire when the operator says so, not automatically.
- **Writer:** `meshctl revoke <node>` (and `reinstate <node>`). The
  orchestrator itself never auto-revokes — revocation stays a human
  decision (approvals stay human, per §20 rules).
- **Enforcement points (all read-side, opt-in by strictness):**
  1. *Admission gate (default on):* orchestrator checks the tombstone
     KV before granting a slot; revoked nodes simply win no auctions.
  2. *Roster gate (default on):* revoked nodes are excluded from
     `roster_nodes()` discovery, so nobody routes work to them.
  3. *Bus gate (opt-in, strict mode):* when NATS_CONF is regenerated
     with `gen_nkeys.py --revoke <node>`, the server config drops the
     user entry entirely — the connection is refused at the transport
     level on next reconnect. This is the only "hard" revocation and it
     costs a conf regeneration; keep it opt-in so a mistake in the
     tombstone KV never bricks a node's connectivity silently.
- **Grace semantics:** revocation is about *future work*. In-flight
   tasks are allowed to complete unless strict mode is used; the DLQ
   records the abort path for audit.
- **Tests (before merge, per standing rules):** revoke → no auction
   wins + absent from roster; reinstate → rejoins on next heartbeat;
   strict conf regeneration → connect refused; tombstone TTL honoured.

### 21.4 Remaining hardening backlog (ranked, each stays small)

1. **Token removal (post-migration cleanup).** Once all nodes carry
   seeds: drop the token-mode branch from compose (`NATS_TOKEN:?`
   guard becomes `NATS_CONF:?`), keep `dev-token` only in tests.
   Small, mechanical, last.
2. **M11 — e2e artifact pinning:** pip-audit + trivy in CI (Makefile
   `ci` target gains `make audit`); pinned `nats:2.10-alpine` digest
   in compose instead of tag.
3. **Container hardening (opt-in profile):** a compose override file
   (`compose.hardened.yml`) that adds `read_only: true`, `cap_drop`,
   `no-new-privileges`, and the docker sandbox sidecar for tier-2
   nodes. Base compose stays simple (OpenClaw gene #3).
4. **Per-task isolation (future, M7 follow-up):** NATS multi-account
   or per-node account JWTs for true per-task subject isolation
   (closes the `mesh.task.*.events` wildcard from H4). Only if the
   mesh ever runs genuinely untrusted nodes — otherwise complexity
   without a threat.
5. **DICE orientation (research flag):** hardware-rooted identity is
   the endgame the blog points at; on current consumer
   hardware there is no TPM-backed attestation story today. Recorded
   as direction, not a near-term item: NKey seeds are today's
   stand-in; if a node ever ships with TPM2, the seed file would be
   replaced by a seed *sealed to* the TPM — same interfaces, better
   root. (DICE composition: ROM-measured boot chain → CDI → identity;
   nothing in our stack measures boot state, so full DICE is out of
   scope and honestly so.)

### 21.5 What we deliberately do NOT do

- No central orchestration-owned sandbox enforcement.
- No certificate authority / mTLS (NKeys already give per-node identity).
- No automatic revocation heuristics ("misbehaviour detection") — a
  human decides, from evidence, per §20's approvals-stay-human rule.
- No per-message authorization layer in Python — enforcement stays at
  the NATS server where it cannot be bypassed by client bugs.

### 21.6 Lesson from OpenClaw: autonomy is not an attack surface

OpenClaw nearly died because convenience shipped as an unauthenticated
surface: open tool/webhook endpoints, missing auth by default, sandboxing
as an afterthought — "open" was treated as a feature. §21.1 protects the
mesh's genes; this section makes sure that protection cannot be abused as
an excuse. The rule set:

1. **Autonomy limits *control*, never *authentication*.** Nothing in
   §21.1 relaxes who may talk to the bus. Every ingress is NKey-
   authenticated (H4, done). "No central kill switch" means no remote
   process control — it never means unauthenticated entry points.
2. **Every listening surface is loopback-bound by default.** The
   terminal-facing agent services (HTTP/monitoring/CLI sockets) bind
   `LAN-IP` on the host (H2 pattern). Anything reachable beyond
   localhost requires an explicit, reviewed config change — same rule
   that already forced `NATS_TOKEN:?` (H3) and the empty pull
   whitelist (secure-by-default).
3. **Optional sandboxing is per *deployment*, not per *mood*.** A node
   that declares tier 1 (full terminal) does so in its own config,
   consciously, once. There is no code path where the agent "just
   disables" a sandbox to get a task done. If a task class keeps
   failing under the sandbox, that is a bug report, not a bypass.
4. **Tool exposure follows the same default-deny.** Terminal-capable
   nodes expose their tools over the mesh subject map only (scoped in
   §21.2); there are no side HTTP APIs for tools, no webhook surface.
   If we ever need one, it inherits rule 2 (loopback + auth) before it
   ships.
5. **Secrets never leave the seed-file + `.env` pattern.** No new
   credential class gets invented per feature; anything that needs
   one, reuses NKey identity or a gitignored file with a fail-closed
   load path.

Concrete audit hook: before any PR that adds a *listening* surface
(HTTP, socket, webhook, new bus subject accepting external payloads),
the PR description must state bind address, auth requirement, and which
§21 rule applies — same weight as the existing every-deny-path-gets-a-
test rule from §20.
## 22. Model-to-model file exchange and shared code repos (design only)

The mesh currently moves *information* (task envelopes, results) but not
*bytes*. Two concrete wants: (a) a model produces a file another model
needs (coder writes a patch, planner wants to read it; critic wants the
diff, not a summary), and (b) multi-step coding tasks need a shared
working tree several models can build on — a repo, not a message.

### 22.1 File exchange over the bus (M17 concept)

Design: a KV-backed artifact store with chunking, no new services.

- **Bucket, not subject.** A `memnc-artifacts` KV bucket (NKey-scoped,
  same pattern as the roster bucket) holds file manifests + chunks.
  Subjects stay control-plane only; bulk bytes live in the KV, like the
  roster and task ledger already do.
- **Manifest + chunks.** Producer publishes `artifact.publish` with a
  manifest: `artifact_id` (hash of content), `sha256`, `size`,
  `mime/name`, `task_id`, `produced_by`. Content goes into KV keys
  `a/<artifact_id>/chunk/<n>` (e.g. 256 KiB chunks). Content-addressing
  gives free dedupe and integrity: consumers verify the hash, always.
- **Capability line, not global read.** Artifacts are task-scoped: only
  nodes holding a claim on the task (or the orchestrator) are granted
  KV access to that artifact prefix. This is §21's trust tiers applied
  to data: default-deny on read, and a revoked node (M7 tombstone) sees
  nothing — its NKey no longer authenticates.
- **Size ceiling.** Hard cap per artifact (start: 50 MB) and per task
  (start: 200 MB total). KV is not a CDN; anything bigger belongs in
  the repo-sharing path (§22.2), not the bus.
- **Delivery flow.** `artifact.publish` (manifest) -> claim-holder
  pulls chunks -> `artifact.ack` with sha256 verification result ->
  reputation bookkeeping (a wrong-hash publish counts against the
  publisher's model reputation — same ledger the claim loop already
  uses).
- **Worker integration.** `artifact fetch <id>` / `artifact put <path>`
  as meshctl verbs + agent tools, so a coder can hand its patch to the
  critic without ever touching the Telegram channel or the host
  filesystem. Workspace stays the execution sandbox; the bus is the
  transport.
- **Deliberately NOT (§21.5 spirit):** no resumable multi-day
  transfers, no pub/sub streaming of binaries, no third-party object
  storage dependency. KV with TTL cleanup covers the mesh's scale.

### 22.2 Shared repos for coding tasks (M18 concept, research first)

The real want behind "models send each other files": several roles
working in one repo across task steps. Three candidate architectures,
to be evaluated against mesh constraints (no central service beyond
NATS, NKey auth, loopback defaults, every node may go away):

1. **Task-local git on the claiming node (recommended starting
   point).** The node that claims a CODING task hosts a bare repo for
   the task's lifetime (plain `git init --bare` in its workspace, plus
   `git daemon`/`git http-backend` bound to the host's docker network
   only — never a public port, §21 rule 2). Other nodes clone via an
   authenticated per-task URL; the claim ledger doubles as the
   authority for "who may push". On DONE, the final tree is packaged
   as an artifact (§22.1) or the repo URL is recorded in the task
   result. Pro: zero new infrastructure, follows the claim model,
   fails with the node (task fails with it, consistent with retries).
   Con: cross-node *concurrent* edits need task decomposition that
   avoids two writers on one tree — a planning constraint, not a
   transport one.
2. **Node-hosted long-lived git server.** One designated node (or the
   host) runs a persistent git server (e.g. `git daemon` or
   soft-serve/gitea) for all tasks. Pro: repos survive task retries,
   natural place for cross-task memory of code. Con: a central
   service to keep alive, an authority question (who owns the repo?
   who may push? — needs the ACL ledger, not ad-hoc), and it breaks
   the "no new listening surfaces without a §21 audit hook" rule
   unless designed as one (bind address, auth, tier).
3. **Peer-to-peer git (git-remote-helper over the bus, e.g. a
   `nats://` remote helper).** Git's remote-helper interface lets any
   transport back a repo; a helper that tunnels `git send-pack` /
   `fetch-pack` over NATS request-reply would make *the bus itself*
   the git network. Pro: no extra service at all, pure mesh. Con:
   most moving parts, hardest to secure (pack negotiation is a
   protocol inside our protocol), and the mesh's reliability model
   (retries, revocation) does not map cleanly onto a live pack
   session. Interesting for later; not for first build.

**Decision framework (for the implement step):** concurrency (do two
roles ever edit the same tree simultaneously? if the orchestrator
serializes CODING per task, option 1 is sufficient), persistence
(how long must a repo outlive a task?), blast radius (what does a
malicious push destroy?), and operational cost (what new process must
stay healthy?). Start with option 1 + §22.1 artifacts; option 2 only
if cross-task repo memory becomes a real want; option 3 remains
research reading (git-remote-helper docs, `gitremote-helpers(7)`),
not implementation.

Security notes carried from §21: repo/auth transport rides NKey
authentication, push rights derive from the claim ledger (no new
credential class, §21 rule 5), the git surface binds to the docker
network only, and artifact hashing (sha256) plus reputation apply the
same "bad actor degrades" model the claim loop already enforces.

## 23. Mesh API / control plane (design only)

kubectl-style mesh API: one verb surface (`task list/detail/submit/cancel`,
`node list/detail/drain`, `discuss`, `listen`, `channel *`, `trace follow`,
`llauncher *`, `grant`) reachable from every frontend — `meshctl`, Telegram,
web UI, later a model-mediated NL agent. See
`docs/mesh-api-concept.md` for the full concept.

Key shape:

- **Topology**: hybrid — reads answered by any node via NATS anycast
  (queue-group `mesh-api`), writes forwarded to the orchestrator as single
  writer. Failover via presence-KV (M8), no leader election; round-robin
  master election only a later optimization (CAS-KV).
- **Channels / teams**: `mesh.api.channel.<team>.*` subject namespaces;
  channel context per frontend (`/channel <team>`), per-team `discuss` and
  `listen`.
- **Human as peer**: the human joins a channel with a `role: human` identity
  (observe / join / mediate), reuses the ask/WAIT_INPUT path (`mesh.clarify`);
  human messages are ordinary channel events agents can react to.
- **Trace following**: `trace follow <task>` streams reasoning steps/verdicts
  from trace-proxy records through the API to any frontend.
- **Subjects (additive)**: `mesh.api.request.<verb>` (anycast),
  `mesh.api.reply.<request_id>`, `mesh.api.events` (JetStream), channel
  namespaces. Same `MeshEnvelope` (+ `request_id`, `reply_subject`,
  `served_by`).
- **Security**: existing ACL; verbs classified read/write/admin; M8 D3
  submitter-signature on mutating calls; no new credentials.
- **Resource management (v2)**: `node detail` aggregates heartbeat/queue/GPU
  (llancherer)/reputation; `node drain` + worker scale for graceful
  maintenance.

Non-goals: no task placement by VRAM math (claim loop stays
reputation-driven), no HTTP mesh-to-mesh, no federation in v1.

Rollout sketch: M11a reads + `listen` via JetStream consumer; M11b write path
+ Telegram as API client; M11c web UI proxy + resource views; M11d
model-mediated API agent.
