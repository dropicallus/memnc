# Operations

## Deploy

```bash
cp .env.example .env        # inference URL, models, token
mkdir -p workspace
docker compose up --build -d
docker compose ps
```

Check health:

```bash
export NATS_URL=nats://127.0.0.1:4222 NATS_TOKEN=dev-token
meshctl doctor
```

`doctor` reports NATS/JetStream, the stream, the KV bucket, and whether each
configured model exists on the endpoint. It exits non-zero if a model is
missing or the endpoint is unreachable.

## Daily commands

| Command | Purpose |
|---|---|
| `meshctl submit "..."` | create a task, prints `task_id` |
| `meshctl tail <id>` | follow the state/event timeline |
| `meshctl state <id>` | full snapshot as JSON |
| `meshctl tasks` | list active and terminal tasks |
| `meshctl doctor` | connectivity + model availability |

## Logs

All components emit one JSON object per line to stderr. Useful fields:
`task_id`, `trace_id`, `role`, `node`, `state`, `event`, `duration_ms`.

```bash
docker compose logs -f orchestrator worker-coder
docker compose logs orchestrator | jq 'select(.task_id=="t_...")'
```

## Runbook

### Task stuck in a stage

1. `meshctl state <id>` — check `state`, `last_action.at`, `attempt`.
2. `docker compose logs worker-<role>` — look for `command received`,
   `result published`, timeouts, or model errors.
3. The reaper re-dispatches after `role_timeout + ORCHESTRATOR_GRACE_S`
   (default: coder 630 s). No manual action is required unless the worker is
   down.
4. If the worker is down, restart it; JetStream redelivers unacked commands.

### Task FAILED

`meshctl state <id>` shows `error` and the event history. Common causes:

- `invalid <role> output` — the model did not return the required JSON twice
- `worker timed out` — raise the role timeout or use a faster model
- `max revisions reached` — the critic rejected every revision
- `artifact apply failed` — path escape or oversized file

### Task CANCELLED

Cancellation is supported by the FSM but not exposed in `meshctl` v1. Publish
an envelope with `kind=event` and `payload.event=cancel` on the task's event
subject, or add a CLI command.

### DLQ

Inspect `mesh.dlq`:

```bash
docker compose exec nats nats sub mesh.dlq --count 10
```

Messages land there when a task is unknown, a stage exhausts retries, or the
orchestrator cannot parse a result. Fix the cause before replaying.

### Reset state (destructive)

```bash
docker compose down -v         # drops NATS JetStream data and KV
rm -rf workspace/*
```

### Worker tool loop is slow or looping

- Lower `MAX_TOOL_STEPS` (default 12).
- Disable tools (`MCP_ENABLED=false`) for models without reliable tool calling.
- Keep `MAX_CONCURRENCY=1` per GPU; scale with replicas instead.

### Model selection

Per-role models are independent. Typical split:

- planner: a reasoning-capable general model (7–14B)
- coder: a code-tuned model (7–32B)
- critic: a general model, ideally different from the coder

`meshctl doctor` verifies presence; `INFERENCE_BASE_URL` can point to a
different host per node.

## Node operations

- Remove a node: stop its compose stack. Its durable consumer remains; pending
  commands are redelivered to another replica of the same role.
- Add capacity: `docker compose up -d --scale worker-coder=2`, or join a new
  host with `docker-compose.node.yml`.
- NATS is the only shared dependency; use a NATS cluster for HA in production.
- Inference endpoints are not managed by this repo.

## Resource limits

Compose sets per-service `mem_limit`, `cpus` and `pids_limit`. Adjust for your
host. The containers run read-only with only `/workspace` and `/tmp` writable.

## Security

Full shell execution is enabled by default. Restrict exposure:

- never mount the Docker socket or host home directories
- keep the workspace bind mount scoped to disposable output
- set `ALLOW_SHELL=false` for review-only or untrusted tasks
- rotate `NATS_TOKEN`; put NATS behind a private network

## E2E live test (T0)

One command for the full self-organization run (submit -> orchestrator stopped ->
workers claim via CAS -> critique -> artifact executed and output-checked):

    MESHCTL=.venv/bin/meshctl ./scripts/e2e.sh --selforg --expect "hello from the mesh" \
        "Create hello.py in the workspace that prints 'hello from the mesh'"

Useful flags: `--artifact PATH` (explicit artifact instead of newest workspace .py),
`--fresh` (wipe workspace first), `--timeout SECONDS`, `--stuck-minutes N` (fail fast
when the state stops changing; default 10). Exit codes: 0 PASS, 1 FAILED/CANCELLED,
2 timeout, 3 submit error, 4 stuck, 5 artifact check failed.
