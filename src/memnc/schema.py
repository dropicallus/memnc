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
# M15: owner-routed submissions — each orchestrator only handles submissions
# addressed to its own node, so two symmetric orchestrators never double-handle.
SUBJECT_SUBMIT_NODE = "mesh.task.submit.{node}"
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

# T6: node roster — one live NodeInfo per node in a TTL KV bucket. TTL gives
# presence: a node that stops heartbeating falls out of the roster on its own.
ROSTER_BUCKET = "roster"
ROSTER_KEY = "roster.{node_id}"
ROSTER_TTL = 60  # seconds; worker heartbeats refresh every ~20s

# --- T7.5: model demand planning (plan §14.7) -------------------------------
# When a task's needs.model is loaded nowhere but present on a node's local
# disk (Ollama blob store), the orchestrator raises a ModelRequest. A human
# approves it (meshctl approve, later the Telegram gate) and the owning node
# loads the model from disk — no network download. If no node has it on disk
# either, the request falls back to a "pull" proposal (T7 path).
SUBJECT_MODEL_DECISION = "mesh.model.decision"
MODEL_REQUESTS_BUCKET = "model_requests"
MODEL_REQUEST_KEY = "mreq.{request_id}"

# T2: human intervention over an approved channel (Telegram observer). The
# orchestrator is the only subscriber; every command carries the acting
# agent_id and is granted-checked (deny-by-default, acl.check_registry_write)
# before it touches the mesh. Response is request-reply JSON.
SUBJECT_CONTROL = "mesh.control"


class ControlCommand(BaseModel):
    """T2: one human intervention command, issued via an approved channel."""

    action: Literal["submit", "cancel", "retry", "stop_worker", "decide"]
    agent_id: str  # who acts — grant-checked against the M9 registry
    task: str = ""  # submit only
    task_id: str = ""  # cancel / retry / decide only
    node: str = ""  # stop_worker only
    approved: bool | None = None  # decide only
    gate: str = ""  # submit only: "always" | "fail" | "never"
    reason: str = ""

    def gate_value(self) -> Literal["always", "fail", "never"]:
        """Validated gate for submits; unknown values fall back to never."""
        return self.gate if self.gate in ("always", "fail", "never") else "never"


class TaskNeeds(BaseModel):
    """T6: capability constraints a node must satisfy to claim a task."""

    vram_gb: float = 0.0  # 0 = no VRAM requirement
    model: str = ""  # exact model name ("" = any)
    roles: list[str] = Field(default_factory=list)  # empty = any role


class NodeInfo(BaseModel):
    """T6: a node's advertised capabilities (refreshed by its workers)."""

    node_id: str
    roles: list[str] = Field(default_factory=list)
    models: list[str] = Field(default_factory=list)
    disk_models: list[str] = Field(default_factory=list)  # T7.5: loadable, not loaded
    vram_gb: float = 0.0
    gpu: str = ""
    load: float = 0.0
    updated_at: str = Field(default_factory=now_iso)

    def key(self) -> str:
        return ROSTER_KEY.format(node_id=self.node_id)

    def merge(self, other: "NodeInfo") -> "NodeInfo":
        """Union of two heartbeat entries for the same node (multi-process)."""
        return NodeInfo(
            node_id=self.node_id,
            roles=sorted(set(self.roles) | set(other.roles)),
            models=sorted(set(self.models) | set(other.models)),
            disk_models=sorted(set(self.disk_models) | set(other.disk_models)),
            vram_gb=self.vram_gb or other.vram_gb,
            gpu=self.gpu or other.gpu,
            load=max(self.load, other.load),
            updated_at=now_iso(),
        )


def satisfies(needs: TaskNeeds | None, node: NodeInfo | None, role: str) -> bool:
    """Pure claim-gate predicate for T6.

    A task without needs is unconstrained. A task WITH needs is fail-closed:
    no live roster entry for the claiming node means the claim is denied.
    """
    if needs is None:
        return True
    if node is None:
        return False
    if needs.roles and role not in needs.roles:
        return False
    if role != "coder":
        # VRAM/model constraints target the executing claim; infrastructure
        # roles (planner/critic) run fixed per-node models.
        return True
    if needs.model and needs.model not in node.models:
        return False
    if needs.vram_gb and node.vram_gb < needs.vram_gb:
        return False
    return True


class ModelRequest(BaseModel):
    """T7.5: a human-gated proposal to make a model available for a task."""

    request_id: str
    model: str
    task_id: str = ""
    action: Literal["load", "pull"] = "load"
    status: Literal["pending", "approved", "rejected"] = "pending"
    nodes_disk: list[str] = Field(default_factory=list)  # nodes holding it on disk
    created_at: str = Field(default_factory=now_iso)
    decided_by: str = ""
    decided_at: str = ""

    def key(self) -> str:
        return MODEL_REQUEST_KEY.format(request_id=self.request_id)


def plan_model_request(
    needs: TaskNeeds | None, nodes: list[NodeInfo], task_id: str = ""
) -> ModelRequest | None:
    """Pure T7.5 planner: decide whether a model request must be raised.

    Returns None when nothing is needed: no model constraint, or the model is
    already loaded on some live node (the claim market serves the task).
    Otherwise the request proposes "load" (model on some node's disk) or
    "pull" (nowhere on disk — T7 download path).
    """
    if needs is None or not needs.model:
        return None
    if any(needs.model in node.models for node in nodes):
        return None
    disk_nodes = sorted({node.node_id for node in nodes if needs.model in node.disk_models})
    return ModelRequest(
        request_id=new_id(),
        model=needs.model,
        task_id=task_id,
        action="load" if disk_nodes else "pull",
        nodes_disk=disk_nodes,
    )


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
        # KV keys only allow [-_./a-zA-Z0-9]; transliterate everything else
        # (e.g. ':' or '[]' in agent ids) so put/get use the same valid key.
        safe = "".join(c if c.isalnum() or c in "-_./" else "-" for c in self.agent_id)
        return GRANT_KEY.format(agent_id=safe)


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
    # M11: agents-as-submitters recursion depth. depth 0 = human/meshctl top
    # task; an agent re-submitting from inside a task passes depth+1, and the
    # orchestrator rejects anything at or above Settings.max_submit_depth.
    depth: int = 0
    # M11 ledger-based self-claiming: claimable tasks are NOT dispatched by
    # the orchestrator. They sit in RECEIVED until an agent CAS-claims them
    # (bus.try_claim); the orchestrator only referees the claim.
    claimable: bool = False
    # T6 resource-aware claim: capability constraints a claiming node must
    # satisfy (see satisfies() below). None = unconstrained (legacy tasks).
    needs: "TaskNeeds | None" = None
    # T3 human approval gate carried over from the submission.
    gate: Literal["always", "fail", "never"] = "never"

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
    # M11: recursion depth (mirrors TaskSubmission.depth) + self-claiming.
    depth: int = 0
    claimable: bool = False
    claimed_by: str = ""  # agent that CAS-claimed this task ("" = unclaimed)
    # M14: when set, the task pipeline is worker-driven — only this role may
    # claim it and the finishing worker advances the snapshot itself.
    next_role: str = ""
    # M15: node whose orchestrator owns this task (submit routing + reaping).
    origin_node: str = ""
    # T6: carried over from TaskSubmission so the claim gate sees the needs.
    needs: "TaskNeeds | None" = None
    # T3 human approval gate: when the critic's verdict should park the task
    # for a human decision instead of auto-advancing.
    #   always — every critique verdict parks the task
    #   fail   — only a rejection parks it (instead of auto-revise)
    #   never  — fully automatic (legacy behavior, default)
    gate: Literal["always", "fail", "never"] = "never"
    # T3: set while the task sits parked at the CRITIQUE gate awaiting a
    # human approve/reject (meshctl gate or Telegram /approve /reject).
    needs_human: bool = False
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
