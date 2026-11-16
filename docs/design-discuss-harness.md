# DiscussHarness — multi-model debate as a coder harness (14.14 item 4)

Status: DESIGN (2026-11-16 night slice) — implemented in the same commit with tests.

## Why

The harness ladder (14.14) ends at "multi-model debate on the same contract".
The weak->strong proof showed the product of a coder is workspace state; the
debate harness makes the *plan* the debated object and keeps the product
contract unchanged: proposals are debated as text, the winner is executed by
the existing in-proc tool loop, and only `HarnessResult` (diff + summary)
comes back out.

## Phases (one pass, no loops beyond a single critique round in v1)

1. **Propose** — every model in `discuss_models` answers the coder prompt once
   (plain chat, no tools, run in parallel with per-call timeouts). Empty or
   failed answers are dropped; zero valid proposals -> `ok=False`.
2. **Critique** — each model sees all proposals and returns a critique/ranking
   (plain text, one round). Failed critiques are skipped, never fatal.
3. **Judge** — one `judge_model` (default: first discuss model) returns
   `WINNER: <model>` plus a short rationale. Unparseable judge output falls
   back to the first valid proposal (deterministic, no re-roll). A winner
   whose proposal is invalid falls back the same way.
4. **Execute** — `ToolLoopHarness(model=winner, ...)` runs on the workspace
   with the same tools/execute_tool the worker would use. The final
   `HarnessResult` is the executor's, with the summary prefixed
   `[discuss] winner=<model>; <judge rationale, truncated>` and
   `tool_events` kept from the tool loop.

## Configuration

- `Settings.discuss_models: list[str]` (JSON env list, default empty).
  Empty falls back to `[model_coder]` — the debate degenerates to a single
  proposal and still works (documented, tested).
- `Settings.judge_model: str = ""` — empty uses the first discuss model.
- Worker wiring: `harness_coder = "discuss"` (or payload `harness`) routes
  coder runs through it; the worker passes `backend`, `tools`, `execute_tool`
  the same way the in-proc path does. Non-coder roles are untouched.

## Cost caveat (deliberate, documented)

2K+1 extra inference calls + the executor loop vs one call for inproc. This
harness is for hard tasks where a wrong plan wastes an entire tool loop; it is
not a default. Bench arm C2/D evaluation decides whether the cost pays.

## Guardrails

- No new authority path: the executor is the same tool loop with the same
  tools; proposals/critiques are data, never executed.
- Timeout budget: the whole harness shares `coder_timeout_s`; proposal and
  critique phases get per-call timeouts derived from it, the executor gets
  what remains. Every wait_for failure degrades, never blocks.
- Tests: sync tests + `asyncio.run`, no `pytest.mark` decorators (repo
  content-filter rule); fake backends only, no model downloads.
