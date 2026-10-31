"""Wire protocol models shared by every mesh component."""

from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


def new_id() -> str:
    """Sortable-enough unique id (time prefix + random suffix)."""
    return f"{int(time.time() * 1000):x}{uuid.uuid4().hex[:12]}"


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Role(str, Enum):
    PLANNER = "planner"
    CODER = "coder"
    CRITIC = "critic"


class Kind(str, Enum):
    COMMAND = "command"
    RESULT = "result"
    EVENT = "event"
    HEARTBEAT = "heartbeat"


class TaskState(str, Enum):
    RECEIVED = "RECEIVED"
    PLANNING = "PLANNING"
    CODING = "CODING"
    CRITIQUE = "CRITIQUE"
    REVISE = "REVISE"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_STATES = {TaskState.DONE, TaskState.FAILED, TaskState.CANCELLED}

SUBJECT_SUBMIT = "mesh.task.submit"
SUBJECT_EVENTS = "mesh.task.{task_id}.events"
SUBJECT_CMD = "mesh.agent.{role}.cmd"
SUBJECT_RESULT = "mesh.agent.{role}.result"
SUBJECT_HEARTBEAT = "mesh.node.{node}.heartbeat"
SUBJECT_DLQ = "mesh.dlq"


class Envelope(BaseModel):
    """Every message on the mesh."""

    v: int = 1
    id: str = Field(default_factory=new_id)
    task_id: str
    trace_id: str
    correlation_id: str | None = None
    kind: Kind
    role: Role | None = None
    node: str | None = None
    state: TaskState | None = None
    attempt: int = 0
    timeout_ms: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    ts: str = Field(default_factory=now_iso)

    @field_validator("task_id", "trace_id")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value:
            raise ValueError("must be non-empty")
        return value


class PlanStep(BaseModel):
    step: int
    description: str
    files: list[str] = Field(default_factory=list)


class PlannerOutput(BaseModel):
    plan: list[PlanStep] = Field(default_factory=list)
    notes: str = ""

    @field_validator("plan")
    @classmethod
    def _non_empty(cls, value: list[PlanStep]) -> list[PlanStep]:
        if not value:
            raise ValueError("plan must contain at least one step")
        return value


class FileArtifact(BaseModel):
    path: str
    content: str


class CoderOutput(BaseModel):
    summary: str = ""
    files: list[FileArtifact] = Field(default_factory=list)
    commands: list[str] = Field(default_factory=list)


class CriticIssue(BaseModel):
    file: str | None = None
    line: int | None = None
    severity: Literal["minor", "major", "blocker"] = "major"
    issue: str
    fix: str | None = None


class CriticOutput(BaseModel):
    approved: bool
    summary: str = ""
    issues: list[CriticIssue] = Field(default_factory=list)


ROLE_OUTPUT: dict[Role, type[BaseModel]] = {
    Role.PLANNER: PlannerOutput,
    Role.CODER: CoderOutput,
    Role.CRITIC: CriticOutput,
}


class ResultPayload(BaseModel):
    ok: bool
    output: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    prompt_version: str | None = None
    tool_steps: int = 0


class TaskSubmission(BaseModel):
    task: str
    context: str = ""
    max_revisions: int | None = None

    @field_validator("task")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("task must be non-empty")
        return value


class HistoryEntry(BaseModel):
    at: str = Field(default_factory=now_iso)
    state: TaskState
    event: str
    detail: str = ""


class TaskSnapshot(BaseModel):
    """Durable workflow state, stored in the NATS KV bucket."""

    task_id: str
    trace_id: str
    task: str
    context: str = ""
    state: TaskState = TaskState.RECEIVED
    attempt: int = 0
    revisions: int = 0
    max_revisions: int = 3
    plan: PlannerOutput | None = None
    code: CoderOutput | None = None
    critique: CriticOutput | None = None
    history: list[HistoryEntry] = Field(default_factory=list)
    seen_result_ids: list[str] = Field(default_factory=list)
    completed_commands: list[str] = Field(default_factory=list)
    completed_tool_calls: list[str] = Field(default_factory=list)
    tool_results: dict[str, str] = Field(default_factory=dict)
    last_action: dict[str, Any] | None = None
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)
    error: str | None = None

    def record(self, event: str, detail: str = "") -> None:
        self.history.append(HistoryEntry(state=self.state, event=event, detail=detail))
        if len(self.history) > 100:
            self.history = self.history[-100:]
        self.updated_at = now_iso()
