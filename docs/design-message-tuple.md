# Design: structured message tuple (TalkHop communication)

Source: TalkHier (Wang et al., arXiv 2502.11098, peer-reviewed: IEEE TAI) —
every inter-agent event is a tuple (Message, Background, Intermediate Output),
not free text. This note designs the memnc adaptation. Design-first: no code
in this slice beyond what is specified here.

## Current state

Role communication today is free text plus role JSON: the planner derives its
prompt from `TaskSubmission.task` + `context`, the coder and critic each
receive re-derived prose prompts, and nothing between hops is persisted
beyond the task snapshot history. The arm C2/D design (docs/design-arm-c2d.md,
D3) already calls for tuples + plan-in-KV; this note makes the schema concrete
so it can ship independently of C2/D.

## Proposed schema

```python
class Background(BaseModel):
    """Persistent, shared context for one task. Written once per hop,
    read by every subsequent hop. Never re-summarized per hop."""
    task: str                      # the original submission task
    context: str = ""              # submitter-provided context
    plan: PlannerOutput | None = None # planner's persisted plan
    assumptions: list[str] = Field(default_factory=list)

class IntermediateOutput(BaseModel):
    """The producing role's working result, superseded by the next hop."""
    role: Role
    summary: str = ""
    files: list[FileArtifact] = Field(default_factory=list)
    issues: list[CriticIssue] = Field(default_factory=list)

class MessageTuple(BaseModel):
    """One hop in the debate pipeline."""
    message: str                   # the instruction/question for the receiver
    background: Background         # read-only for the receiver
    intermediate_output: IntermediateOutput | None = None
```

## Persistence

- The `Background` for a task is stored in the existing workflow_state KV
  bucket (per-task snapshot record gains a `background` field, or a companion
  key `background.<task_id>` — implementation detail for the build slice;
  companion key preferred to keep snapshots backward-compatible).
- Writers: orchestrator on RECEIVED (task+context) and after the planner hop
  (plan + assumptions). Coder and critic are read-only consumers.
- `intermediate_output` is NOT persisted — it lives only inside the envelope
  payload of the current hop, exactly TalkHier's per-event field. The next
  hop's result replaces it. The snapshot history already records outcomes.

## Prompt integration

`build_user_prompt` gains an optional tuple argument. When present, the prompt
is assembled as three labeled sections (MESSAGE / BACKGROUND /
INTERMEDIATE OUTPUT) instead of re-derived prose; role JSON output contracts
are unchanged. Workers without a tuple (older envelopes, selforg tasks) fall
back to the existing prompt — no hard dependency, so rollout is per-role.

## Explicit non-goals

- No new KV bucket. workflow_state stays the single durable store.
- No change to envelope routing subjects or the FSM — tuples ride in the
  existing `Envelope.payload`.
- No critic-consumes-tuple behavioral change in this slice: arm C behavior
  must be byte-identical when the tuple path is off (it is opt-in via
  a settings flag, default off, turned on together with arm D1).

## Build order (one small slice)

1. [DONE] Schema models above in `schema.py` + unit tests (round-trip, fallback).
2. [DONE] Orchestrator writes background to workflow_state (`background.<task_id>`
   companion key, best-effort save on submit and on every planner hop in both
   the orchestrator result path and the worker self-pipeline).
3. [DONE] `build_user_prompt` tuple section + worker read path behind the flag
   (`Settings.tuple_prompts`, default off; dispatch attaches the tuple when on,
   workers validate it and fall back to prose on any validation error).
4. Enable flag only when arm D1 lands (success criterion lives in
   docs/design-arm-c2d.md).

## Cost caveat

Zero extra model calls: the tuple is assembled from data the pipeline already
produces (submission, planner output, previous result). The only cost is KV
writes, which the pipeline already performs for snapshots.
