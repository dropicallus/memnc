# memnc

[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-34d399?style=flat-square)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-22d3ee?style=flat-square)](pyproject.toml)
[![Tests](https://github.com/dropicallus/memnc/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/dropicallus/memnc/actions/workflows/ci.yml)
[![No cloud required](https://img.shields.io/badge/cloud-never-fb7185?style=flat-square)](#security-model)

**A multi-agent mesh that runs entirely on your own hardware.**
NATS is the only backbone. A durable state machine drives `planner → coder → critic`
across worker nodes. Every worker runs a local MCP tool server (filesystem + shell)
and talks to its own local inference endpoint (Ollama or any OpenAI-compatible API).
Claimable tasks run **worker-driven**: the claiming worker advances the pipeline
itself via CAS role-gated handoffs — the orchestrator is a spectator you can kill
mid-run without stopping the pipeline.

No agent framework. No cloud dependencies. No model-serving code.

<p align="center">
  <img src="assets/architecture.svg" alt="memnc mesh architecture" width="920">
</p>

## Why

- **Your GPUs, your models, your data** — nothing leaves the network.
- **The mesh is the memory** — every stage is attributed in a durable ledger
  (`meshctl ledger`), not in an agent's chat history.
- **Resource-aware claiming** — workers advertise VRAM, GPU and on-disk models;
  the claim gate routes tasks to nodes that can actually run them.
- **Human in the loop, on your phone** — a Telegram observer streams task events
  and takes gated control commands (`/submit`, `/cancel`, `/retry`, `/stop worker`);
  the model pull path is approval-gated.

## Quick start

```bash
cp .env.example .env        # set INFERENCE_BASE_URL and MODEL_*
mkdir -p workspace          # bind-mounted at /workspace in every container
docker compose up --build -d

python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

export NATS_URL=nats://127.0.0.1:4222 NATS_TOKEN=dev-token
.venv/bin/meshctl doctor

TASK_ID=$(.venv/bin/meshctl submit "Create hello.py that prints hello from the mesh")
.venv/bin/meshctl tail "$TASK_ID"
```

More capacity is one flag: `docker compose up -d --scale worker-coder=3`.
Two-machine mesh, worker-driven claimable tasks and grant/revoke semantics are
covered in [`docs/operations.md`](docs/operations.md).

## What's live

| Feature | Status |
|---|---|
| Worker-driven pipeline (CAS claim, role gate) | ✅ shipped |
| Cross-node claiming + grants (gateway, sticky revoke) | ✅ shipped |
| Resource-aware claim gate (VRAM / GPU / roles) | ✅ shipped |
| Telegram observer + gated control commands | ✅ shipped |
| Web task console (roster, task detail, cancel/retry) | ✅ shipped |
| Debate trace: coder/critic revisions in `/trace` | ✅ shipped |
| Model lifecycle: pull whitelist, reputation, retirement gate | ✅ shipped |
| Model routing visible (`/trace` shows node:model per role) | ✅ shipped |
| Roster advertises `disk_models` (Ollama `/api/tags`) | ✅ shipped |
| Experience graph: observation deposits + trace query (§14.13 steps 0–2) | 🚧 in progress |
| Role-specific LoRA adapters + Fisher-Merge (§19.1 B2/B4) | 🚧 planned |
| Leader synthesis layer, MLPO pattern (§19.1 B3) | 🚧 planned |
| Orchestrator model-demand planning → approval gate | 🚧 in progress |
| Console v2: live view + problem surface (§14.10) | 🚧 planned |
| Robustness: worker crash, timeout/requeue, orchestrator restart | 🚧 planned |
| LAN model blob sharing between nodes | 🚧 planned |

Design details and staged plan: [`docs/roadmap.md`](docs/roadmap.md).
Research-informed build order (26-paper corpus → build/prototype/observe triage,
brownfield status): [`docs/plan.md`](docs/plan.md) §19.

## Documentation

| Document | Contents |
|---|---|
| [`docs/plan.md`](docs/plan.md) | The specification: architecture, wire protocol, orchestrator, workers, deployment, security |
| [`docs/roadmap.md`](docs/roadmap.md) | Built / next / later items and the experience-graph outlook |
| [`docs/architecture.md`](docs/architecture.md) | Component and data-flow view |
| [`docs/protocol.md`](docs/protocol.md) | NATS subjects and envelope format |
| [`docs/operations.md`](docs/operations.md) | Running and operating the mesh |
| [`docs/recovery.md`](docs/recovery.md) | How the codebase was rebuilt from `.pyc` remnants |

New documentation is consolidated into these files, not added as new files.

## Configuration

Everything is environment-driven; see [`.env.example`](.env.example) for the full list.

| Variable | Default | Purpose |
|---|---|---|
| `NATS_URL` | `nats://127.0.0.1:4222` | NATS server |
| `NATS_TOKEN` | — | shared token auth |
| `INFERENCE_PROVIDER` | `ollama` | `ollama` (native) or `openai` (compatible) |
| `INFERENCE_BASE_URL` | — | endpoint root, per-node |
| `MODEL_PLANNER` / `MODEL_CODER` / `MODEL_CRITIC` | — | per-role model names |
| `NODE_VRAM_GB` / `NODE_GPU` | `0` / — | advertised in the roster, used by the claim gate |
| `MESH_GATE` | `false` | registry gate; `MESH_GRANT_NODES` bootstraps grants |
| `MAX_REVISIONS` | `3` | critic rejection cycles before failing |
| `SELF_PIPELINE` | `true` | workers advance claimable tasks without orchestrator dispatch |
| `MODEL_PULL_WHITELIST` | stock role models + `gemma*` | fnmatch patterns for the approval-gated model pull path; empty list closes it |

Models with native tool calling work best. Without tools the worker still works:
the coder returns file contents as JSON and the orchestrator applies them.

## Security model

`run_shell` is intentionally unrestricted — the isolation boundary is the container:
non-root user, `cap_drop: ALL`, `no-new-privileges`, read-only root filesystem,
CPU/memory/PID limits, only the workspace bind mount is writable, no Docker socket.
Treat a mesh worker as a trusted-but-dangerous sandbox. Set `ALLOW_SHELL=false` to
disable command execution entirely. Inbound message content (Telegram, NATS) is
treated as untrusted data, never instructions; secrets live in the gitignored `.env`.

## Local development

```bash
docker run -d --rm -p 4222:4222 nats:2-alpine -js
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest            # unit tests, no NATS required
./scripts/e2e.sh                      # full end-to-end against a running mesh
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
  telegram.py      # Telegram observer + control commands
  console.py       # web task console
docs/              # architecture, protocol, operations
```

## Contributing & License

See [CONTRIBUTING.md](CONTRIBUTING.md). Apache License 2.0 — see
[LICENSE](LICENSE). Copyright in each contribution belongs to its commit
author, recorded automatically in git history; there are no per-file headers.
