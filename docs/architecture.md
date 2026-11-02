# Architecture

## Components

### NATS backbone

One JetStream-enabled NATS server (or cluster) carries every message:

- core pub/sub for heartbeats and live event tailing
- JetStream pub/sub for durable commands/results/events
- request/reply for task submission
- KV bucket `workflow_state` for workflow snapshots and per-command tool caches

Three traffic classes:

| Subject | Delivery | Purpose |
|---|---|---|
| `mesh.task.submit` | request/reply | submit a task, returns `{task_id}` |
| `mesh.agent.<role>.cmd` | JetStream durable pull, queue group per role | work assignment |
| `mesh.agent.<role>.result` | JetStream durable pull | worker output |
| `mesh.task.<id>.events` | JetStream | append-only task timeline |
| `mesh.node.<node>.heartbeat` | core NATS | liveness, model and busy state |
| `mesh.dlq` | JetStream | unprocessable/exhausted messages |

`mesh.task.submit` is deliberately excluded from the stream subjects:
JetStream acknowledges stored messages to their reply inbox, which would race
the orchestrator's response with a `PubAck`.

### Orchestrator

A single process owning the workflow state machine. It:

1. accepts submissions and creates a snapshot (state `RECEIVED`)
2. dispatches role commands, one stage at a time
3. consumes results, validates role output, applies coder artifacts, and
   advances the FSM
4. reaps stalled stages and recovers in-flight work after a restart

All transitions go through `fsm.transition()`, a pure function with no I/O.
Snapshots are written before dispatch (intent) and after every transition
(outcome), so a crash always leaves a recoverable state.

```
RECEIVED ──▶ PLANNING ──▶ CODING ──▶ CRITIQUE ──approved──▶ DONE
                ▲            ▲            │
                │            └──REVISE◀───┘ rejected (revisions < max)
                │
                └── retry (attempts < max)

any active state ──timeout/cancel/exhausted──▶ FAILED | CANCELLED
```

### Workers

One generic process per role (`ROLE=planner|coder|critic`). Lifecycle:

1. connect, ensure stream/KV, start the stdio MCP server
2. pull a command from the role's durable consumer
3. render role prompt + context from the command payload
4. chat with the inference endpoint, executing MCP tools when requested
5. parse the final JSON, apply coder artifacts locally, publish a result, ack

Idempotency: every tool call is keyed by `(command_id, tool, args_hash)` and
cached in KV (`tools.<task_id>.<command_id>`). A redelivered command replays
cached tool output instead of re-running the side effect. The cache is deleted
after the result is published and acked.

### MCP tool servers

Each worker spawns `python -m memnc.mcp.server` over stdio. Tools are plain
JSON-in/JSON-out strings:

| Tool | Arguments | Behaviour |
|---|---|---|
| `read_file` | `path`, `max_bytes` | path-confined read, truncation flag |
| `write_file` | `path`, `content` | path-confined atomic write |
| `list_dir` | `path`, `depth` | relative listing |
| `search_text` | `pattern`, `glob`, `max_results` | regex search |
| `run_shell` | `cmd`, `cwd`, `timeout_s` | full shell, timeout + output cap + audit log |

Path confinement resolves symlinks and rejects anything outside
`WORKSPACE_ROOT`.

### Inference backends

`ChatBackend` normalizes both providers to `content + ToolCall[]`:

- Ollama native `/api/chat` (`tools`, `keep_alive`, `num_ctx`, `num_predict`)
- OpenAI-compatible `/v1/chat/completions` (`tool_calls`, bearer auth)

Shared retry/backoff on 429/5xx and transport errors; no retry on 4xx. The
worker's tool loop is provider-agnostic.

## Data flow

```
meshctl submit ─▶ orchestrator ─put snapshot─▶ KV
                       │
                       └─command─▶ mesh.agent.planner.cmd ─▶ planner worker
                                                                  │
                       KV ◀──snapshot/tool cache───────────────┘
                       │
                       └◀─result── mesh.agent.planner.result
                       │
                       └─command─▶ coder ... ─▶ critic ... ─▶ DONE
```

## Durability and recovery

- Stream: `max_age=7d`, `max_msgs=1M`, 120 s duplicate window keyed by envelope
  id.
- Consumers: `worker-<role>` and `orchestrator-results`, explicit ack,
  `max_deliver=3`, `ack_wait=1.5×` the role timeout.
- Snapshots: KV `workflow_state`, key `task.<id>`, history 10.
- On restart the orchestrator reloads active snapshots and resumes `REVISE`
  transitions immediately; in-flight stages wait for redelivery or the
  timeout reaper.
- Results are matched to `last_action.command_id`; stale or duplicate results
  are ignored.
- The reaper runs every 5 s and re-dispatches stages older than
  `role_timeout + ORCHESTRATOR_GRACE_S`.

## Identity, ACLs and the capability gate (M9)

Three layers, following Cotal/Synadia (sender identity on the wire) and ormica
(permission chain):

- **Transport auth**: the NATS token (`NATS_TOKEN`) is the outer layer — it
  polices who can connect at all. For multi-node deployments the broker is
  additionally locked down per account/user (NATS ACLs, JWT/operator mode);
  memnc reads `NATS_TOKEN` and never trusts identity fields inside payloads.
- **Capability gate** (`MESH_GATE=1`): a write to the registry KV is
  authorized against a grant ledger (KV `mesh.grants`, deny-by-default).
  Grants follow the ormica chain — `root` (the orchestrator) may issue grants,
  `chain` lets an agent write only its own records, `auto` (self-declared) is
  never sufficient. Issuance/check logic is pure and NATS-free (`memnc/acl.py`).
- **Pinned inbox**: each agent answers work on `mesh.agent.<agent_id>.work`
  (its own inbox) plus the anycast `mesh.agent.work`; sender-carried subjects
  (`mesh.<space>.<mode>.<target>.<sender>`) embed the identity in the subject
  itself so a mismatched claim is detectable (`acl.check_sender`).

Operate with `meshctl grant <agent_id> [--level root|chain] [--ttl-s N]`,
`meshctl grants`, `meshctl revoke <agent_id>`. With the gate off (local dev)
registry writes are unrestricted — the deny-by-default check lives in
`acl.check_registry_write` and is enforced by `Bus.advertise_capability`.

## Scaling

- Replicas of a role share a durable consumer (queue-group semantics): one
  delivery per command.
- The orchestrator is a singleton for v1; state lives in KV, so restart is
  cheap. Horizontal orchestration sharding is future work.
- Multi-host: workers on each host join the same `NATS_URL`; each host can
  point at its own inference endpoint.
- `MAX_CONCURRENCY` is 1 by default because inference is the bottleneck and a
  single GPU should not be oversubscribed.
