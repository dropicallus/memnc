# memnc

A lightweight, distributed multi-agent mesh. NATS is the only backbone; a
durable state-machine orchestrator assigns `planner`, `coder` and `critic`
roles to worker nodes; each worker runs a local MCP tool server (filesystem +
shell) and talks to a local inference endpoint (Ollama or any OpenAI-compatible
API).

No agent framework, no cloud dependencies, no model serving code.

```
             ┌──────────────────┐
 meshctl ───▶│  NATS (Docker)   │◀──────────────┐
             │  pub/sub         │               │
             │  JetStream + KV  │               │
             │  request/reply   │               │
             └───┬──────────┬───┘               │
                 │          │                   │
   mesh.agent.*.cmd        │ mesh.agent.*.result│
                 ▼          ▼                   │
        ┌──────────────────────────┐            │
        │  Orchestrator            │            │
        │  FSM + durable snapshots │            │
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

## Quick start

Prerequisites: Docker + Docker Compose, Python 3.11+ for `meshctl`, and a
running inference endpoint.

```bash
cp .env.example .env
# edit .env: INFERENCE_BASE_URL and MODEL_*
#   host Ollama:          http://host.docker.internal:11434
#   OpenAI-compatible:    INFERENCE_PROVIDER=openai, INFERENCE_BASE_URL=..., INFERENCE_API_KEY=...

mkdir -p workspace          # bind-mounted at /workspace in every container
docker compose up --build -d
docker compose ps

python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# meshctl runs on the host and talks to the published NATS port
export NATS_URL=nats://127.0.0.1:4222
export NATS_TOKEN=dev-token
.venv/bin/meshctl doctor

TASK_ID=$(.venv/bin/meshctl submit "Create hello.py that prints hello from the mesh")
.venv/bin/meshctl tail "$TASK_ID"
.venv/bin/meshctl state "$TASK_ID"
```

The coder and orchestrator both write artifacts into `./workspace`, so the
result is visible on the host as soon as the coder step completes.

## Inference

The mesh only connects to inference; it never runs a model server.

| Variable | Meaning |
|---|---|
| `INFERENCE_PROVIDER` | `ollama` (native `/api/chat`) or `openai` (`/v1/chat/completions`) |
| `INFERENCE_BASE_URL` | endpoint root, e.g. `http://192.168.1.20:11434` |
| `INFERENCE_API_KEY` | bearer token for OpenAI-compatible endpoints |
| `MODEL_PLANNER` / `MODEL_CODER` / `MODEL_CRITIC` | per-role model names |
| `INFERENCE_TIMEOUT_S` | request timeout |
| `NUM_CTX` / `NUM_PREDICT` | context window / max output tokens |

Each node uses its own `INFERENCE_BASE_URL`, so a node with a local Ollama
instance can point at `http://localhost:11434` (or
`host.docker.internal` from a container) while another points at a remote
endpoint.

Models with native tool calling work best. Without tools the worker still
works: the coder returns file contents as JSON and the orchestrator applies
them.

## Scaling out

More capacity on one host:

```bash
docker compose up -d --scale worker-coder=3
```

### Two-machine mesh (gated)

Machine A owns NATS + orchestrator; machine B joins with workers and an
adapter agent. The capability gate is on, and machine B's agents are granted
automatically via heartbeat bootstrap (`MESH_GRANT_NODES` on machine A).

**Machine A** (starts broker, orchestrator, workers; allows `node-b-*` to
join):

```bash
MESH_GATE=true MESH_GRANT_NODES='node-b-*' docker compose up --build -d
```

**Machine B** (workers use B's own local Ollama via `host.docker.internal`;
set `AGENT_MODEL` to a model Ollama on B actually has):

```bash
NATS_URL=nats://<host-a-ip>:4222 NODE_NAME=node-b MESH_GATE=true \
  docker compose -f docker-compose.node.yml up --build -d
```

Verify (from machine A, with `NATS_URL`/`NATS_TOKEN` exported or `.env`):

```bash
meshctl grants        # node-b-planner / -coder / -critic / -agent appear
                      # within ~5 s of node B starting (heartbeat bootstrap)
meshctl registry      # node-b-agent's capability record is listed
meshctl submit "write a hello world in python" --watch
meshctl tail <task_id>   # events carry node=<node-a|node-b-…> per dispatch
```

Workers of the same role share a JetStream durable pull consumer, so each
command is delivered to exactly one worker in the group — a task may execute
on either machine. Which node ran a stage shows up in `meshctl tail <id>` /
`meshctl submit --watch` as a `result.executed_on` event (the executing
worker's node name), and in the worker's own logs on machine B
(`docker compose -f docker-compose.node.yml logs -f worker-coder`).

Revoking stays sticky: `meshctl revoke node-b-coder` blocks that agent even
though heartbeats keep arriving (bootstrap never overwrites an existing grant
record — delete is respected until manually re-granted).

Without the gate (`MESH_GATE=false`, the default), both machines still work —
the gate only guards registry writes, not queue-group work delivery.

## Configuration

Everything is environment-driven; see `.env.example` for the full list.

| Variable | Default | Purpose |
|---|---|---|
| `NATS_URL` | `nats://127.0.0.1:4222` | NATS server |
| `NATS_TOKEN` | — | shared token auth |
| `ROLE` | `coder` | worker role |
| `NODE_NAME` | hostname | identity in logs/heartbeats |
| `WORKSPACE_ROOT` | `/workspace` | tool sandbox and artifact root |
| `MAX_REVISIONS` | `3` | critic rejection cycles before failing |
| `MAX_ATTEMPTS` | `3` | retries per stage |
| `MAX_TOOL_STEPS` | `12` | model tool-call rounds per command |
| `PLANNER_TIMEOUT_S` / `CODER_TIMEOUT_S` / `CRITIC_TIMEOUT_S` | 180 / 600 / 180 | per-stage budgets |
| `ORCHESTRATOR_GRACE_S` | `30` | slack before the reaper re-dispatches |
| `ALLOW_SHELL` | `true` | enable the `run_shell` MCP tool |
| `SHELL_TIMEOUT_S` | `120` | per-command timeout |
| `APPLY_ARTIFACTS` | `true` | write coder output files to the workspace |

## Security model

`run_shell` is intentionally unrestricted (no allowlist/denylist). The
isolation boundary is the container:

- runs as a non-root user with `cap_drop: ALL`, `no-new-privileges`, read-only
  root filesystem, CPU/memory/PID limits
- only the workspace bind mount is writable; no Docker socket
- the model can execute arbitrary code and reach anything the container can
  reach — treat a mesh worker as a trusted-but-dangerous sandbox and do not
  mount host paths you care about

Set `ALLOW_SHELL=false` to disable command execution entirely.

## Local development

```bash
docker run -d --rm -p 4222:4222 nats:2-alpine -js
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

export NATS_URL=nats://127.0.0.1:4222
.venv/bin/python -m memnc.orchestrator &
ROLE=planner .venv/bin/python -m memnc.worker &
ROLE=coder .venv/bin/python -m memnc.worker &
ROLE=critic .venv/bin/python -m memnc.worker &
.venv/bin/meshctl submit "Create hello.py"
```

Run the suite:

```bash
.venv/bin/python -m pytest            # unit tests, no NATS required
docker run -d --rm -p 4223:4222 nats:2-alpine -js
MEMNC_TEST_NATS=nats://127.0.0.1:4223 .venv/bin/python -m pytest -m integration
./scripts/smoke.sh                    # against a running mesh + real model
```

## Layout

```
src/memnc/
  schema.py        # envelope + payload contract
  fsm.py           # pure state machine
  bus.py           # NATS stream, KV snapshots, consumers
  orchestrator.py  # FSM runtime, recovery, timeout reaper
  worker.py        # pull loop, agent loop, checkpoints
  agent.py         # tool loop + JSON parsing
  backends/        # ollama + openai-compatible clients
  mcp/             # stdio tool server/client (fs + shell)
  cli.py           # meshctl
docs/              # architecture, protocol, operations
```

See `docs/architecture.md`, `docs/protocol.md` and `docs/operations.md` for
details.
