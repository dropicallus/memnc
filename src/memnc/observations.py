"""Observation deposit: the write unit of the mesh's experiential graph.

Design origin: Frank's eclipse-score/mcp-servers `libs/score-context`
(ContextDelta + Provenance) and `packages/context-discipline`
(OutcomeRecord). Plan reference: docs/memnc-mesh-plan.md section 14.12.

This slice defines the *schema* only — what a worker/orchestrator can
deposit about a finished task. Persisting observations into a graph
store and the trace-following attention are later slices (§14.12 steps
2+). Schema versioning is mandatory: old payloads must never silently
distort ranking (open point (c) in §14.12).
"""

from __future__ import annotations

from itertools import combinations

from pydantic import BaseModel, Field

from memnc.schema import TERMINAL_STATES, TaskSnapshot, now_iso

OBSERVATION_SCHEMA_VERSION = 1

# KV charset is [-_./a-zA-Z0-9]; cooperation edge ids use "|" and "+" which
# are both safe, and models are joined with a fixed separator.
_PAIR_SEP = "|+|"


class TaskObservation(BaseModel):
    """One finished task as experienced by the mesh.

    Everything here is already recorded on the TaskSnapshot (T4-Rest);
    this model is the normalized, versioned projection of it.
    """

    schema_version: int = OBSERVATION_SCHEMA_VERSION
    task_id: str
    trace_id: str
    task: str
    # outcome: task terminal state (done / failed / cancelled)
    outcome: str
    # route: role -> "node:model", exactly as recorded on the snapshot
    route: dict[str, str] = Field(default_factory=dict)
    revisions: int = 0
    critic_verdict: str | None = None
    critique_summary: str = ""
    error: str | None = None
    origin_node: str = ""
    observed_at: str = Field(default_factory=now_iso)

    def model_names(self) -> list[str]:
        """Distinct models that served on this task, sorted for stability."""
        models = {r.split(":", 1)[-1] for r in self.route.values() if ":" in r}
        return sorted(models)

    def synergy_pairs(self) -> list[str]:
        """Unordered cooperation pairs ("a|+|b") of distinct models.

        Same model on two roles produces no self-edge (§14.12).
        """
        return [_PAIR_SEP.join(pair) for pair in combinations(self.model_names(), 2)]

    def kv_id(self) -> str:
        """KV-safe observation id (charset [-_./a-zA-Z0-9])."""
        return f"obs.{self.task_id}.{self.schema_version}"


def observation_from_snapshot(snapshot: TaskSnapshot) -> TaskObservation | None:
    """Build an observation from a terminal task snapshot.

    Returns None when there is nothing to learn from: the task never
    executed a step (no route recorded) or has not reached a terminal
    state yet. Non-terminal snapshots are not observed — the outcome is
    not final.
    """
    if snapshot.state not in TERMINAL_STATES:
        return None
    if not snapshot.route:
        return None
    critique = snapshot.critique
    return TaskObservation(
        task_id=snapshot.task_id,
        trace_id=snapshot.trace_id,
        task=snapshot.task,
        outcome=snapshot.state.value.lower(),
        route=dict(snapshot.route),
        revisions=snapshot.revisions,
        critic_verdict=(
            ("approved" if critique.approved else "rejected") if critique else None
        ),
        critique_summary=(critique.summary if critique else ""),
        error=snapshot.error,
        origin_node=snapshot.origin_node,
    )
