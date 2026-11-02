"""Environment-driven configuration shared by orchestrator, workers and MCP."""

from __future__ import annotations

import socket
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
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
    nats_url: str = "nats://127.0.0.1:4222"
    nats_token: str | None = None
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
    planner_timeout_s: int = 180
    coder_timeout_s: int = 600
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
    model_planner: str = "qwen2.5:7b"
    model_coder: str = "qwen2.5-coder:7b"
    model_critic: str = "llama3.1:8b"
    num_ctx: int = 8192
    num_predict: int = 4096

    log_level: str = "INFO"

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
