"""Environment-driven configuration shared by orchestrator, workers and MCP."""

from __future__ import annotations

import shlex
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
    # P4 slice 3: wake-on-backlog. When the backlog driver finds planned
    # work and wol_mac is set, it sends a WOL magic packet (throttled by
    # wol_min_interval_s) to wake the cold-reserve host ("" = disabled).
    wol_mac: str = ""
    wol_min_interval_s: float = 300.0

    # T6: node capabilities advertised into the roster KV (roster.<node>).
    # node_vram_gb = VRAM the mesh may plan around (0 = unknown/none);
    # node_gpu = free-form label (e.g. "test-gpu-a").
    node_vram_gb: float = 0.0
    node_gpu: str = ""
    nats_url: str = "nats://127.0.0.1:4222"
    nats_token: str | None = None
    # Topology (framework, not any specific host setup): nats_url accepts a
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

    # Backend lifecycle (memnc.control): adapters own the process side, memnc
    # owns the contract. llauncher_bin is looked up on PATH; ollama_url is the
    # daemon's native API (not the /v1 shim).
    llauncher_bin: str = "llauncher"
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_unit: str = "ollama.service"

    # Compose/containers: a container counts as a backend only if it carries
    # docker_label, is named in docker_backends (comma list), or runs a known
    # inference-server image. That keeps mesh infra (nats, workers) out of the
    # backend list without hardcoding names.
    docker_bin: str = "docker"
    docker_label: str = "memnc.backend"
    docker_backends: str = ""

    # Native llama-server processes. llamacpp_units lists systemd user units
    # that should appear even while stopped (start/restart need a unit; a
    # hand-started process can only be stopped).
    llamacpp_bin: str = "llama-server"
    llamacpp_units: str = ""

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
    # P4 slice 2 (node update logic): when enabled, a worker refuses any
    # dispatch command whose mesh_version differs from its own code
    # fingerprint — stale nodes stop claiming until they upgrade.
    enforce_node_version: bool = False
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
    # now -- see docs/roadmap.md §14.14). Harnesses share one contract
    # (memnc.harnesses.Harness), so weak->strong handoff stays routing policy.
    harness_coder: str = "inproc"
    # DiscussHarness (14.14 item 4): models that debate the coder prompt as
    # text before one winner is executed by the in-proc tool loop. JSON env
    # list; empty falls back to [model_coder] (single-proposal degenerate case).
    discuss_models: list[str] = []
    # Judge for the debate; empty uses the first discuss model.
    judge_model: str = ""
    critic_timeout_s: int = 180
    orchestrator_grace_s: int = 30
    # 15.4.5 gap: a CAS claim whose worker died (restart/crash) must not
    # orphan the task. The reaper frees claims older than this many seconds;
    # must comfortably exceed the longest legitimate step
    # (critic_timeout_s + orchestrator_grace_s).
    claim_ttl_s: float = 900
    # P5.1 lease by liveness: a claim whose node has been silent this long is
    # released immediately instead of waiting for claim_ttl_s. Workers beat
    # every 20 s, so 90 s = three missed beats.
    heartbeat_stale_s: float = 90.0
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
    # Structured message tuples (docs/roadmap.md): when on, the
    # orchestrator attaches a MessageTuple to every dispatch payload and the
    # worker renders the prompt as MESSAGE/BACKGROUND/INTERMEDIATE OUTPUT
    # sections instead of re-derived prose. Off = byte-identical arm C.
    tuple_prompts: bool = False
    # 15.3 item 5: experience store producers. Path of the file-backed
    # ExperienceStore; observation deposits append success/failure bullets
    # only when the flag is on (opt-in, like tuple_prompts).
    # 15.4.5 DONE-gate hardening: when on, a DONE transition requires at
    # least one executed command or tool call on the snapshot. Opt-in per
    # deployment (DONE_REQUIRES_ACTIVITY=1): pure writing tasks legitimately
    # finish with zero tools.
    done_requires_activity: bool = False

    # 15.4.5 gap 2: after the critic verdict, run deterministic acceptance
    # checks parsed from the plan's per-step ``acceptance`` fields. A failing
    # check blocks the verdict from standing and drives the revision loop
    # with the check output as the issue text. No-op when no plan step
    # carries parseable criteria.
    acceptance_gate: bool = True

    # 15.4.3 error-aware retry: on a model-level failure (timeout / 5xx /
    # invalid output) the orchestrator rotates the failed role to another
    # advertised model from the roster instead of re-picking the broken one.
    # Human retry intent (model_override) always wins.
    model_rotate_on_fail: bool = True
    # Ship-gate V2 (docs/roadmap.md): mechanical deliverable
    # checks, opt-in like done_requires_activity. 0 = check off.
    verify_min_lines: int = 0
    verify_min_files: int = 0
    # Model management twin: staleness kills a node in the
    # allocation matrix; the model_manager announces health on this interval.
    twin_stale_s: int = 30
    health_interval_s: int = 5
    # llauncher MCP server command line (stdio), run by the model_manager.
    # Real default per Hermes config: <venv python> -m llauncher.mcp_server.
    llauncher_mcp_cmd: str = (
        "~/.hermes/tools/llauncher/venv/bin/python -m llauncher.mcp_server"
    )

    def mcp_argv(self) -> list[str]:
        """Split llauncher_mcp_cmd into argv, expanding ~ in argv[0]."""
        argv = shlex.split(self.llauncher_mcp_cmd)
        if not argv:
            raise ValueError("llauncher_mcp_cmd is empty")
        argv[0] = str(Path(argv[0]).expanduser())
        return argv
    experience_store_path: str = "data/experience-store.json"
    # 14.13 Step 6: the dreaming job is dormant until the operator flips
    # this flag (the human sign-off the roadmap design asks for).
    dreaming_enabled: bool = False
    experience_from_observations: bool = False
    # 14.15.10: pheromone evaporation. The orchestrator applies decay every
    # pheromone_decay_interval_s seconds (0 disables the job); rho is the
    # fraction of accumulated pheromone that evaporates per interval.
    pheromone_decay_interval_s: int = 3600
    pheromone_rho: float = 0.1
    # 14.15.10 selection consumption: consult the pheromone field when a task
    # starts (and re-select after a failed verify) so measured success/cost
    # data, not config, picks the strategy edge. weak_planner discounts
    # planner-heavy edges while no planner-grade model is registered.
    pheromone_select: bool = True
    pheromone_weak_planner: bool = True

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
    # 15.4.6 VRAM fit gate: reject loads that fit no card; MoE models that
    # need a hot expert cache only schedule on nodes declaring capability.
    vram_fit_gate: bool = True
    moe_hot_cache_capable: bool = False
    moe_hot_cache_models: list[str] = Field(default_factory=list)
    # T7: consecutive critic rejections before a retirement proposal is raised.
    retire_threshold: int = 3

    log_level: str = "INFO"
    # Bench checkers import model-written code. Off by default; a node that
    # runs bench with untrusted output turns this on to run each checker in a
    # child process with a minimal environment and a wall-clock limit.
    bench_isolate: bool = False

    # T1: read-only Telegram observer. Empty TELEGRAM_ALLOW = mirror to nobody
    # (the observer refuses to start, so a leaked token alone does nothing).
    telegram_bot_token: str | None = None
    telegram_allow: str = ""
    telegram_allow_users: str = ""
    telegram_poll_s: int = 25
    telegram_verbose: bool = False
    # Quieter digest: suppress routine progress-state transitions (RECEIVED/
    # PLANNING/CODING/CRITIQUE/REVISE); only terminal + WAIT_INPUT + failure
    # details still broadcast. Set TELEGRAM_QUIET_PROGRESS=0 for the full stream.
    telegram_quiet_progress: bool = True
    telegram_roster_updates: bool = False
    telegram_pending_requests: bool = False

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
