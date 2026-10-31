# Wire protocol

Everything is JSON, versioned by the envelope's `v` field. Validate on receive;
reject to `mesh.dlq` when unparseable.

## Envelope

```json
{
  "v": 1,
  "id": "01J8Z...",              // unique message id, used for JetStream dedupe
  "task_id": "t_01J8Z...",
  "trace_id": "01J8Z...",
  "correlation_id": "cmd_id",    // command id this result answers
  "kind": "command | result | event | heartbeat",
  "role": "planner | coder | critic | null",
  "node": "node-a",
  "state": "CODING | null",
  "attempt": 1,
  "timeout_ms": 600000,          // execution budget for commands
  "payload": {},
  "ts": "2026-10-31T18:00:00Z"
}
```

`task_id` and `trace_id` must be non-empty. Field names are stable; new
optional fields may be added without bumping `v`.

## Subjects

| Subject | Kind | Persistence |
|---|---|---|
| `mesh.task.submit` | request/reply | none (excluded from stream) |
| `mesh.agent.<role>.cmd` | command | JetStream |
| `mesh.agent.<role>.result` | result | JetStream |
| `mesh.task.<id>.events` | event | JetStream |
| `mesh.node.<node>.heartbeat` | heartbeat | core NATS |
| `mesh.dlq` | event | JetStream |

## Payloads

### Command (orchestrator → worker)

```json
{
  "task": "original task text",
  "context": "optional extra context",
  "plan": {"plan": [{"step": 1, "description": "...", "files": []}], "notes": ""},
  "code": {"summary": "...", "files": [{"path": "...", "content": "..."}], "commands": []},
  "critique": {"approved": false, "summary": "...", "issues": []},
  "revisions": 1,
  "workspace_root": "/workspace",
  "role": "coder"
}
```

### Planner result

```json
{"ok": true, "output": {"plan": [{"step": 1, "description": "...", "files": ["a.py"]}], "notes": ""},
 "prompt_version": "...", "tool_steps": 0, "error": null}
```

### Coder result

```json
{"ok": true, "output": {"summary": "...", "files": [{"path": "a.py", "content": "..."}], "commands": []}}
```

File paths are relative to the workspace and must not escape it. Artifacts are
applied by both the worker and the orchestrator (idempotent).

### Critic result

```json
{"ok": true, "output": {"approved": false, "summary": "...",
  "issues": [{"file": "a.py", "line": 3, "severity": "major", "issue": "...", "fix": "..."}]}}
```

`approved` is the only required field. A malformed verdict is treated as a
failed critic attempt and retried.

### Failure

```json
{"ok": false, "error": "agent error: ...", "output": {}, "tool_steps": 0}
```

### Heartbeat

```json
{"role": "coder", "node": "node-a", "model": "qwen2.5-coder:7b",
 "busy": false, "task_id": null}
```

## MCP tool responses

All tools return a JSON string:

```json
{"ok": true, "path": "a.py", "content": "...", "truncated": false}
{"ok": true, "exit_code": 0, "stdout": "...", "stderr": "", "truncated": false,
 "timed_out": false, "duration_ms": 12}
{"ok": false, "error": "path escapes workspace: ../x"}
```

Malformed model output is retried once with a correction prompt; a second
failure fails the stage (and eventually the task, after retries).

## Delivery semantics

At-least-once, not exactly-once.

- Commands are acked only after the result is published.
- If a worker dies mid-command, JetStream redelivers it (up to `max_deliver`).
- Tool side effects are protected by the per-command cache
  (`tools.<task_id>.<command_id>`): completed calls are replayed, not re-run.
- A command killed *between* side effect and cache write can still repeat that
  one side effect. This is the accepted gap of full-shell execution with
  at-least-once delivery.
- Results are deduplicated by envelope id and matched against
  `last_action.command_id`; stale results are dropped.
- KV writes are last-write-wins for snapshots. The orchestrator serializes
  transitions with a lock; workers no longer write workflow snapshots, so they
  cannot regress state.
