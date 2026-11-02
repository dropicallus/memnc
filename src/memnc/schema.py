"""Wire protocol models shared by every mesh component."""

from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


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
SUBJECT_ASSIGN = "mesh.agent.{worker}.cmd"
SUBJECT_AUCTION = "mesh.auction.{task_id}.{step_id}"
SUBJECT_BID = "mesh.bid.{task_id}.{step_id}"
SUBJECT_HEARTBEAT = "mesh.node.{node}.heartbeat"
SUBJECT_DLQ = "mesh.dlq"
# --- M10: queryable mesh ledger (hive tracker, plan §0.5 D5) ---
# Core-NATS request/reply, intentionally NOT covered by the JetStream stream
# (same reason as mesh.task.submit: a stream-covered request would be answered
# with the JS PubAck instead of the handler's reply). Any granted agent can
# request the consolidated ledger here; meshctl falls back to direct KV reads
# when no orchestrator is answering.
SUBJECT_LEDGER = "mesh.ledger"

# --- M7: heterogeneous capability registry ---
# External agents (Hermes/pi/Claude Code) advertise what they can do into a
# TTL KV bucket so the mesh can route by capability instead of fixed role.
SUBJECT_CAP_REGISTER = "mesh.registry.register"
SUBJECT_CAP_WITHDRAW = "mesh.registry.withdraw"
SUBJECT_AGENT_WORK = "mesh.agent.work"
# Registry KV bucket lives next to the workflow_state bucket (see bus.py).
REGISTRY_BUCKET = "registry"

# Capability key layout inside the registry KV:
#   registry.<agent_id>.<skill>  ->  Capability JSON, expires by bucket TTL.
CAPABILITY_KEY = "registry.{agent_id}.{skill}"

# Which role prompt a worker uses when it wins a step for a given skill.
SKILL_ROLES: dict[str, Role] = {
    "plan": Role.PLANNER,
    "code": Role.CODER,
    "test": Role.CODER,
    "review": Role.CRITIC,
}


def role_for_skill(skill: str) -> Role:
    return SKILL_ROLES.get(skill, Role.CODER)

# --- M8: trust ledger ---
TRUST_BUCKET = 'mesh_trust'
TRUST_DEFAULT = 1.0
TRUST_MIN = 0.0
TRUST_MAX = 10.0
TRUST_ACCRUE = 0.5
TRUST_DECAY = 0.95


class TrustEntry(BaseModel):
    """M8: A record in the trust ledger for an agent."""
    subject: str
    rating: str
    credit: float = TRUST_DEFAULT
    ts: float = Field(default_factory=time.time)
    attribution: list[str] = Field(default_factory=list)




# --- M9: identity, ACLs, capability gate ---
# Grants live in their own KV bucket (no TTL entry per record: expiry is a
# field on the record, checked by acl.check_registry_write, so revocation is
# a write, not a TTL race).
GRANT_BUCKET = "mesh_grants"
GRANT_KEY = "grant.{agent_id}"

# Per-agent pinned durable inbox: the only work subject bound to one agent
# (the adapter already subscribes to this shape).
SUBJECT_AGENT_INBOX = "mem.agent.{agent_id}.work"  # mem.*: outside MESH-stream coverage — core request/reply on stream-covered subjects is answered by the JetStream PubAck, never by the adapter


class GrantLevel(str, Enum):
    """ormica permission chain: ROOT issues, CHAIN writes own records, AUTO = self-declared (never sufficient)."""

    ROOT = "root"
    CHAIN = "chain"
    AUTO = "auto"


class GrantRecord(BaseModel):
    """M9: an orchestrator-issued capability grant for one agent."""

    agent_id: str
    level: GrantLevel = GrantLevel.CHAIN
    granted_by: str = "orchestrator"
    issued_at: float = Field(default_factory=time.time)
    # Epoch seconds at which the grant stops being valid; 0 = no expiry.
    expires_at: float = 0

    def key(self) -> str:
        return GRANT_KEY.format(agent_id=self.agent_id)


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


class StepStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    AUCTIONING = "auctioning"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class PlanStep(BaseModel):
    """One node in the planner's task DAG.

    Old planners emitted only ``step``/``description``/``files``; the extra
    fields have defaults so those plans still validate and are promoted to a
    sequential DAG by the orchestrator.
    """

    step: int = 1
    description: str
    files: list[str] = Field(default_factory=list)
    id: str = ""
    skills: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    acceptance: str = ""
    status: StepStatus = StepStatus.PENDING
    worker: str | None = None
    attempts: int = 0
    result: str = ""

    @model_validator(mode="after")
    def _defaults(self) -> "PlanStep":
        if not self.id:
            self.id = f"s{self.step}"
        if not self.skills:
            self.skills = ["code"]
        return self


class PlannerOutput(BaseModel):
    plan: list[PlanStep] = Field(default_factory=list)
    notes: str = ""

    @field_validator("plan")
    @classmethod
    def _non_empty(cls, value: list[PlanStep]) -> list[PlanStep]:
        if not value:
            raise ValueError("plan must contain at least one step")
        return value


class WorkerProfile(BaseModel):
    """A live worker and the skills it is willing to bid with."""

    worker_id: str
    node: str
    skills: list[str] = Field(default_factory=list)
    model: str = ""
    busy: bool = False
    capacity: int = 1
    load: float = 0.0
    last_seen: float = 0.0
    at: str = Field(default_factory=now_iso)


class Capability(BaseModel):
    """M7: a skill an external agent advertises into the capability registry.

    Unlike ``WorkerProfile`` (native mesh worker bidding in the auction), a
    ``Capability`` is a static, TTL-expiring declaration of what a
    heterogeneous agent (Hermes, pi, Claude Code, ...) can do and how to reach
    it.  The registry routes work to the agent by this record; ``contact`` is
    whatever the outer harness needs to deliver the task (an MCP endpoint, a
    subject, a CLI shim, ...).
    """

    agent_id: str
    skill: str
    node: str = ""
    model: str = ""
    # How the mesh should hand a task to this agent. Free-form contract the
    # adapter understands (e.g. "mcp://...", "subject:<s>", "cli:<cmd>").
    contact: str = ""
    load: float = 0.0
    capacity: int = 1
    ts: float = Field(default_factory=time.time)

    def key(self) -> str:
        return CAPABILITY_KEY.format(agent_id=self.agent_id, skill=self.skill)

    @classmethod
    def from_key(cls, key: str) -> tuple[str, str] | None:
        """Split a registry KV key back into (agent_id, skill)."""
        parts = key.split(".")
        if len(parts) != 3 or parts[0] != "registry":
            return None
        return parts[1], parts[2]


class AuctionRequest(BaseModel):
    """Published by the orchestrator when a DAG step becomes ready."""

    auction_id: str = Field(default_factory=new_id)
    task_id: str
    step_id: str
    skill: str = "code"
    description: str = ""
    acceptance: str = ""
    attempt: int = 1
    preferences: dict[str, float] = Field(default_factory=dict)
    window_ms: int = 1200


class Bid(BaseModel):
    """A worker's offer for one auction."""

    auction_id: str
    task_id: str
    step_id: str
    worker_id: str
    node: str = ""
    skills: list[str] = Field(default_factory=list)
    model: str = ""
    load: float = 0.0
    capacity: int = 1
    at: str = Field(default_factory=now_iso)


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
    # M10 fan-out: the orchestrator decomposes the planner's multi-step plan
    # into independent subtasks on mesh.task.submit and returns immediately.
    fanout: bool = False

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
    self_assign: bool = False
    # M10 fan-out: parent task that spawned subtasks and aggregates them.
    fanout: bool = False
    subtasks: dict[str, str] = Field(default_factory=dict)  # subtask_id -> step id
    parent_task_id: str = ""
    plan: PlannerOutput | None = None
    code: CoderOutput | None = None
    critique: CriticOutput | None = None
    final_review: PlanStep | None = None
    step_results: dict[str, str] = Field(default_factory=dict)
    inflight: dict[str, dict[str, Any]] = Field(default_factory=dict)
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
