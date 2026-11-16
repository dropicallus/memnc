# Ship-Gate V2 — mechanical deliverable checks

## Problem (observed 4x, 2026-11-16)

Tasks reach DONE with stub deliverables. Root cause chain:

1. Coder claims files; worker stamps `artifact_sizes` (bytes, -1 = missing).
2. Orchestrator only refuses when a claimed file is **missing entirely**.
   A 418-byte stub that exists passes the gate.
3. The critic is an LLM judging the coder's **narrative**; it asserts
   "creates a branch, commits and pushes" without any evidence. Fact #128.

The mesh cannot fix this itself — self-implemented gates keep being stubs.
This is a human-authored PR.

## Design

Mechanical, evidence-based checks in the orchestrator result gate (single
hook point: `_on_result` / `_handle_result`, covers both external results
and the worker self-pipeline, which routes through the same handler).

New per-result evidence, stamped by the worker where artifacts are applied:

- `artifact_sizes: dict[str, int]` (existing; bytes, -1 = missing)
- `artifact_lines: dict[str, int]` (new; non-blank lines, -1 = missing)

New settings (opt-in, same pattern as `done_requires_activity`):

- `verify_min_lines: int = 0` — every claimed coder artifact must have at
  least this many non-blank lines on disk. 0 = off.
- `verify_min_files: int = 0` — a coder result must claim at least this
  many artifacts. 0 = off.

Gate rule (coder results only, in orchestrator after payload validation):

- any `artifact_sizes` value < 0 → fail (existing behavior)
- any `artifact_lines` value < `verify_min_lines` → fail
- number of claimed files < `verify_min_files` → fail

Failure is a mechanical step refusal with the file names and measured
numbers in the error — no narrative trust involved.

## Deliberately out of scope (follow-ups)

- Branch-push verification (`git ls-remote`) — needs git credentials in
  the worker container; separate slice once creds wiring exists.
- Running referenced tests in the workspace — sandboxed execution risk.
- Critic prompt hardening — the critic stays advisory; the gate is
  structural and does not depend on it.

## Rollout

- Merge as PR, tests green.
- node-a: set `VERIFY_MIN_LINES=20` in the worker environment and recreate.
- node-c: needs image refresh on maco — blocked on SSH fix (key rejected,
  authorized_keys mismatch/permissions on the 180).
