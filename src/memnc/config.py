"""Environment-driven configuration shared by orchestrator, workers and MCP."""

from __future__ import annotations

import socket
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .schema import Role


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # identity / transport
    role: Role = Role.CODER
    node_name: str = Field(default_factory=socket.gethostname)
    # Weak->strong routing: node that receives strong-retry tasks after a
    # weak node exhausts its attempt budget ("" = disabled).
    strong_node: str = ""

    # T6: node capabilities advertised into the roster KV (roster.<node>).
    # node_vram_gb = VRAM the mesh may plan around (0 = unknown/none);
    # node_gpu = free-form label (e.g. "test-gpu-a").
    node_vram_gb: float = 0.0
    node_gpu: str = ""
    nats_url: str = "nats://127.0.0.1:4222"
    nats_token: str | None = None
    # Topology (framework, not Frank's setup): nats_url accepts a
    # comma-separated seed list ("nats://a:4222,nats://b:4222") — the
    # client round-robins/reconnects across it. kv_replicas follows the
    # deployment topology: 1 = solo/duo (single JetStream), 3 = mesh
    # (Raft-replicated, needs a >=3-voter NATS cluster).
    nats_kv_replicas: int = 1
    # M11d: model-mediated API agent. Backend is pluggable — any
    # OpenAI-compatible /v1 endpoint works (local llauncher, trace-proxy,
    # LM Studio, cloud). agent_allowed_verbs is the fail-closed allowlist.
    agent_llm_base_url: str = "http://127.0.0.1:4000/v1"
    agent_llm_model: str = "local"
    @property
    def nats_servers(self) -> list[str]:
        """Split NATS_URL into a server pool (comma-separated seed list)."""
        return [u.strip() for u in self.nats_url.split(",") if u.strip()]

    # 14.13 step 5: experience-driven claim gate. A server whose own track
    # record (from deposited observations) is below claim_synergy_min_ratio
    # with at least claim_synergy_min_samples stops claiming that role —
    # the mesh routes by its own history, no central scheduler.
    claim_synergy_gate: bool = True
    claim_synergy_min_samples: int = 3
    claim_synergy_min_ratio: float = 0.3
    agent_allowed_verbs: str = (
        "task.list,task.detail,node.list,node.detail,"
        "cap.list,grant.list,trust.list,ledger.read,task.submit"
    )
    # H4: per-node NKey identity. When set, this seed file replaces the
    # shared token entirely (transport-level identity binding); the token
    # is then ignored. Fail closed: a configured seed path must exist.
    nats_nkeys_seed: str | None = None
    stream_name: str = "MESH"
    kv_bucket: str = "workflow_state"
    nats_name: str | None = None

    # M9: capability gate. When enabled, registry writes require an
    # orchestrator-issued grant (deny-by-default, see memnc/acl.py).
    # Default off so local dev/test meshes stay friction-free; compose
    # and any multi-node deployment set MESH_GATE=1.
    mesh_gate: bool = False
    # M9 grant bootstrap: comma-separated glob patterns of node names whose
    # heartbeat automatically earns their agents a CHAIN grant (e.g.
    # "node-b-*"). Empty = no auto-grant; manual `meshctl grant` only.
    grant_nodes: str = ""

    @field_validator(
        "inference_fallback_provider",
        "inference_fallback_api_key",
        "inference_api_key",
        "nats_token",
        "nats_name",
        mode="before",
    )
    @classmethod
    def _empty_str_to_none(cls, v):
        """Compose interpolates unset vars as '' — treat that as unset."""
        if v == "":
            return None
        return v

    # workspace / tools
    workspace_root: Path = Path("/workspace")
    mcp_enabled: bool = True
    allow_shell: bool = True
    shell_timeout_s: int = 120
    max_output_bytes: int = 65_536
    max_tool_steps: int = 12

    # workflow
    max_concurrency: int = 1
    max_revisions: int = 3
    max_attempts: int = 3
    # M16 mesh-clarify: consult the peer roles once before a question goes
    # to the human; peers that do not answer within the timeout are declined.
    max_clarify_rounds: int = 1
    clarify_timeout_s: int = 120
    planner_timeout_s: int = 180
    coder_timeout_s: int = 600
    # Harness for the coder role: "inproc" (default, the worker's own tool
    # loop) or "pi" (external pi CLI against the workspace; host-side for
    # now -- see docs/roadmap.md 14.14). Harnesses share one contract
    # (memnc.harnesses.Harness), so weak->strong handoff stays routing policy.
    harness_coder: str = "inproc"
    critic_timeout_s: int = 180
    orchestrator_grace_s: int = 30
    apply_artifacts: bool = True
    max_artifact_bytes: int = 200_000

    # M11: agents as submitters + ledger-based self-claiming
    # max_submit_depth = how deep agent-initiated resubmissions may nest
    # (depth 0 = top task; a submission is rejected when depth >= limit).
    max_submit_depth: int = 2
    # When set, workers poll the ledger and CAS-claim claimable RECEIVED
    # tasks themselves instead of waiting for orchestrator dispatch.
    mesh_claim: bool = False
    claim_poll_s: float = 5.0
    # M14: claimed tasks run their pipeline worker-driven — the finishing
    # worker advances the snapshot itself; the orchestrator is a spectator.
    self_pipeline: bool = True

    # inference (external runtime)
    inference_provider: Literal["ollama", "openai"] = "ollama"
    inference_base_url: str = "http://localhost:11434"
    inference_api_key: str | None = None
    inference_timeout_s: float = 180.0
    inference_max_retries: int = 3
    # Fallback inference (e.g. a peer node's local ollama) used when the
    # primary is unreachable. Empty provider disables the fallback.
    inference_fallback_provider: Literal["ollama", "openai"] | None = None
    inference_fallback_base_url: str = ""
    inference_fallback_api_key: str | None = None
    # "primary=fallback,primary2=fallback2" model name remapping.
    inference_fallback_model_map: str = ""
    model_planner: str = "qwen2.5:7b"
    model_coder: str = "qwen2.5-coder:7b"
    model_critic: str = "llama3.1:8b"
    num_ctx: int = 8192
    num_predict: int = 4096
    # T7: model pull whitelist. fnmatch patterns ("qwen*", "gemma-4-31b").
    # Empty list = the pull path is closed (pull decisions are acknowledged
    # but refused); loaded/disk models keep working without any entry.
    model_pull_whitelist: list[str] = Field(default_factory=list)
    # T7: consecutive critic rejections before a retirement proposal is raised.
    retire_threshold: int = 3

    log_level: str = "INFO"

    # T1: read-only Telegram observer. Empty TELEGRAM_ALLOW = mirror to nobody
    # (the observer refuses to start, so a leaked token alone does nothing).
    telegram_bot_token: str | None = None
    telegram_allow: str = ""
    telegram_poll_s: int = 25
    telegram_verbose: bool = False

    def model_for(self, role: Role) -> str:
        return {
            Role.PLANNER: self.model_planner,
            Role.CODER: self.model_coder,
            Role.CRITIC: self.model_critic,
        }[role]

    def timeout_for(self, role: Role) -> int:
        return {
            Role.PLANNER: self.planner_timeout_s,
            Role.CODER: self.coder_timeout_s,
            Role.CRITIC: self.critic_timeout_s,
        }[role]

    def models(self) -> dict[Role, str]:
        return {role: self.model_for(role) for role in Role}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
