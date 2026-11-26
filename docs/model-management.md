# Concept: Model-Management Layer + Node Digital Twin

Status: Draft (incorporate feedback)

## Problem

- Mesh tasks fail because the orchestrator routes blindly: it knows neither VRAM utilization,
  nor which models are currently loaded, nor load times.
- Concrete case: the 31B critic occupied VRAM, the planner (7B) had to reload → timeout.
- Ollama loads on demand and forgets after ~5 min (`keep_alive`) — both uncoordinated.
- There is no layer that manages models per node and loads/unloads them dynamically.

## Research Findings (Prior Art)

### llama-swap (mostlygeek/llama-swap, active, Golang, single binary)
- Transparent proxy in front of arbitrary OpenAI-compatible servers (llama.cpp, vLLM, tabbyAPI).
- **On-demand model switching**: a request with `model=X` loads X and swaps out the wrong
  upstream. Exactly what we need — without our own daemon management.
- **`/running`** — list of currently loaded models → **digital twin source**.
- **`/unload`** — manual unloading (by model ID or all).
- **`ttl`** — automatic unloading after a timeout (configurable per model).
- **`groups`** — several models at the same time; `swap`/`exclusive`/`persistent`:
  - `swap: true` → only one model of the group in VRAM
  - `exclusive: true` → unloads other groups on start
  - `persistent: true` → always stays loaded (e.g. keep the planner small)
- **`aliases`**, `env`, `cmdStop` (Docker/Podman), `${PORT}` dynamic ports,
  `healthCheckTimeout`, `hooks` (preload on start).
- Configuration: a single `config.yaml`.
- **⚠ Invalidated by practical testing (owner): in the owner's own use it was not reliable →
  do NOT choose it as serving layer.** Kept here only as a conceptual reference
  (groups/ttl/swap semantics).

### llauncher (separate project, PRIMARY option)
- **The MCP concept is agent-native** (owner: "The cool thing about llauncher is the
  MCP concept") — registry truth as MCP resource; agents query state instead of
  self-loading into Ollama. Exactly the fix for "agent loads blindly on its own".
- Full control layer as MCP tools: `start_server`, `stop_server`,
  `swap_server`, `cancel_server`, `server_status`, `server_slots`,
  `server_metrics`, `get/set_model_config`, `list_backends`/`list_models`,
  `validate_models` (weights check), `backend_status` (systemd + port +
  health), `get_server_logs`, `read_audit` (ADR-8 audit log).
- llama.cpp-based (`llama-server`), systemd user units, port management,
  slots including prompt text, live inference telemetry.
- **What is missing (MM task):** NATS bridge — map health announce and `load/unload/
  drain` intents from NATS to MCP calls. Then llauncher is the model manager itself,
  no extra daemon needed.
- **Assessment, honestly corrected:** previously insufficiently covered — the agent
  instead loaded models into Ollama itself. llauncher covers lifecycle (start/stop/swap),
  observability (status/slots/metrics/logs/audit) and validation — more than llama-swap,
  and tested on a mixed AMD fleet (discrete and integrated GPUs).

### vLLM (reference for pool management)
- `--gpu-memory-utilization` = "claim at most X% of VRAM" — the central pool principle.
- For large models on shared cards: compute the utilization dynamically from the actually
  free VRAM (NVIDIA forum pattern with an nvidia-smi query at startup).

### Latency-Aware Orchestration (arXiv 2609.03335)
- Prediction-guided runtime for parallel agent workflows on heterogeneous GPUs:
  a predictor estimates activation/loading costs per model × device; the scheduler
  chooses fusion/lifecycle/binding under live pool state and replans from feedback.
- → Long-term direction: instead of hard routing, a cost model (load time + VRAM fit)
  per node/model pair, learned from measured load times.

### "Kubernetes for Models" (DeepSeek research) — Comparison

De-facto control planes for AI workloads, sorted by approach:
- **KServe** — K8s standard for model serving (operator, vLLM/SGLang/Triton).
- **AIBrix** (ByteDance) — LLM inference control plane: routes by the live state of the
  engines, autoscaling by inference metrics, manages models + LoRA via the OpenAI API.
  Conceptually closest to our target picture.
- **Kthena** — K8s-native, prefill/decode disaggregation, zero-downtime updates.
- **llm-d** — distributed inference stack; its routing intelligence (KV-cache aware) can
  now also run **without Kubernetes** — the only candidate with a standalone mode, but a
  heavier stack.
- **Atlas** — engine-agnostic control plane + router: specification → GPU capacity check →
  routing to an OpenAI endpoint. Good as a blueprint for our `mesh.alloc.request` semantics.
- **LLMKube** — operator for heterogeneous hardware (NVIDIA/AMD/Apple Silicon).
- **Taho / Slurm** — a K8s alternative and the classic HPC batch system, respectively;
  irrelevant for us.

**Assessment for memnc:** All K8s-native setups assume a cluster — we have 2–3 homelab
nodes with NATS + Docker Compose; a k3s cluster would be infrastructure overkill, and the
owner explicitly does **not** want to build Kubernetes. But we adopt the concepts directly:
1. **AIBrix "Route by live engine state"** = our planner allocation target (decision based
   on the live twin instead of a static roster).
2. **Atlas semantics** (submit spec → check capacity → route) = the flow of our
   `mesh.alloc.request`.
3. **AIBrix LoRA management** = later relevant for parametric memory (load/unload LoRA per
   knowledge domain instead of swapping base models).
Our path remains: a lean own twin on NATS (MM1–MM5), no K8s dependency.

### Multi-Host Swarms & MCP Orchestration (research 2)

**Swarms/clusters (bottom-up):**
- **mycoSwarm** — mDNS auto-discovery, capability exchange, routing by hardware capability.
  Takeaway: **mDNS as a discovery extension** for us (nodes announce themselves instead of
  a hardcoded roster).
- **Mesh-LLM** — see its own section below (primary reference).
- **Exo** — Apple Silicon focus, MLX backend; only conditionally useful for the iGPU-only host,
  but the discovery pattern is the same.

**llama.cpp cluster managers:**
- **Tightwad** — pools CUDA+ROCm via llama.cpp RPC across machines; killer feature:
  **speculative decoding over the network** (a small model drafts on the weak machine, a
  large one verifies on the strong one → 2–3×). Interesting for the node-a (discrete GPU) +
  supervisor host combination, but complex; note the idea, not now.
- **RPC Manager** — web UI + mDNS discovery + **live telemetry (VRAM, temperature) per
  node**. Confirms: telemetry as first-class twin data.
- **BareMetalRT** — TensorRT-native, NVIDIA-only; irrelevant (AMD).

**MCP as control plane (validates the llauncher path):**
- **apra-fleet** — "what K8s was for containers, for AI agents": machines as fleet members
  via SSH, routing by cost + capabilities, observability. Proves that **MCP-controlled
  multi-host orchestration without K8s is conceived as production-capable** — the same
  architecture direction as llauncher MCP + NATS twin for us.
- **Myrmex Hive** — decentralized agent orchestration framework on an MCP basis,
  **zero inbound ports** (outbound connections only). Exactly our NATS subscriber pattern —
  nodes connect outward to the mesh, no open ports.
- **Argus MCP** — gateway that aggregates several MCP servers behind one endpoint.
  Relevant if the planner should access the llauncher MCP of several nodes at the same time.

**Conclusion:** Confirms our architecture (MCP control + decentralized twin, no K8s). New
takeaways: (1) mDNS discovery instead of a hardcoded roster, (2) telemetry as first-class
twin fields, (3) Tightwad SpecDec as a future option.

### Mesh-LLM (Skippy + Mesh, 2026, Rust/QUIC — reference architecture)
Mesh-LLM shows what this looks like in production and provides concrete building blocks for
our planner-as-allocator approach:

- **Two layers separated**: Skippy (model management, native inference, OpenAI serving) +
  Mesh (discovery, routing, gossip). → For us: model manager (node layer) + orchestrator
  (routing layer), NATS instead of QUIC.
- **Runtime lifecycle intents** (exactly the verbs we need): `load-model` (one-shot),
  `ensure-model` (kept present, with retry — survives transient failures), `unload-model`,
  `drain-model` (in-flight work finishes, then unload). **drain-model is the clean
  replacement for our VRAM blockade chain**: do not cut off requests that are already running.
- **`scan-refresh` / inventory**: the getNode state (config + loaded models) as a sorted
  inventory list → our digital twin counterpart. Owner control runs over a separate control
  channel (for us: a NATS subject with NKey-scoped permissions — the H4 mechanism fits exactly).
- **Dynamic capability knowledge**: nodes share via gossip which models they can currently
  serve; routing decides by the `model` field. For us: `mesh.node.health` + capability
  matrix (model × node × engine).
- **`benchmark tune`** measures load times/throughput per model × node × settings and writes
  recommendations back → prior art for our MM5 cost model (load-time budget instead of
  VRAM fit alone).
- **Disadvantage for us**: Mesh-LLM is a complete product (QUIC, Nostr discovery, its own
  splits protocol) — migrating would replace our NATS/mesh infrastructure. We adopt the
  concepts (intents, drain, inventory, benchmark), not the product.

## Architecture Draft

```
┌─────────────────────────────────────────────────────────┐
│ Orchestrator (Routing)                                  │
│  - Roster: which node can handle which role/models      │
│  - Digital Twin: per node {loaded, VRAM-free, queue}    │
│  - Routing rule: loading < threshold? → prefer node     │
│    with the model loaded; otherwise node with free VRAM │
└──────────────┬──────────────────────────────────────────┘
               │ mesh.node.health (KV/NATS, periodic)
┌──────────────▼──────────────────────────────────────────┐
│ Model manager (one container per node, next to Ollama)  │
│  - sources llama-swap /running + nvidia-smi (VRAM)      │
│  - publishes a health announce every N seconds:         │
│    {node, models_loaded[], vram_free_mb, vram_total_mb, │
│     queue_depth, load_times{model:sec}}                 │
│  - accepts commands: preload/unload/evict               │
└─────────────────────────────────────────────────────────┘
```

### Components

1. **llama-swap as the serving layer per node** (replaces raw Ollama access):
   - `config.yaml` per node: all mesh models with `cmd` (llama-server or vLLM), `ttl`, `groups`.
   - Group strategy on a 16 GB card: `persistent` only for the smallest planner model;
     coder/critic in a `swap: true` group.
   - Advantage: no own daemon code for loading/unloading — llama-swap already does this and
     is proven; we only build the control layer.

2. **Model manager agent** (new, `src/memnc/model_manager.py`):
   - Polls `llama-swap /running` + `nvidia-smi` (VRAM free/total).
   - Publishes `mesh.node.health` on NATS (or KV bucket `node.health`).
   - Endpoints: `preload <model>` (request to llama-swap to start the upstream without
     inference), `unload <model>`, `evict` (largest model out).
   - Measures load times and writes them into the health announce → basis for the routing
     cost model.

3. **Digital twin in the orchestrator**:
   - In-memory state per node: `{node, models_loaded, vram_free, vram_total,
     queue_depth, load_times, last_seen}`.
   - Update: health announces (push) + a node is marked "dead" if it is absent for >30 s.
   - Routing rule (v1, deterministic):
     a) Node where the target model is **already loaded** → prefer.
     b) Otherwise a node with **enough free VRAM** (load time incl. reserve).
     c) Otherwise: `evict` command to the node with the largest resident model,
        then (b).
   - No hard timeout as a workaround anymore — load times flow into the budget.

4. **Ollama special case (transition)**:
   - Ollama has no `/running` API in the llama-swap sense, but `/api/ps` (loaded models +
     `size_vram`) and `/api/generate` with `keep_alive: 0` for unloading. The model manager
     can source Ollama in interim mode until llama-swap is in place.

### Implementation Order (Milestones)

- **MM1** — Model manager reads `/api/ps` + VRAM, publishes the health announce.
  (Digital twin visible, still no routing behavior.)
- **MM2** — Orchestrator collects twin state, logs routing decisions with a rationale
  ("node-a: coder loaded, VRAM free 9 GB → chosen").
- **MM3** — Routing rule v1 active (loaded > free VRAM > evict).
- **MM4** — llama-swap as serving layer on node-a, Ollama migration (config.yaml, groups,
  ttl); the model manager then sources `/running`.
- **MM5** — Cost model: measured load times per model × node → routing takes the
  load-time budget into account instead of VRAM fit alone.

## Planner as Allocator (target picture, owner requirement)

The planner gets **direct access to the mesh** and allocates per task itself — the
orchestrator becomes a pure executor (route + execute), and the decision "which model on
which node" is made by the planner.

### Why This Works
- The planner (7B) only needs a **structured allocation** as output:
  `{"model": "...", "node": "...", "reason": "...", "fallback": "..."}` — that is a tool
  call, not an additional knowledge problem.
- The **dynamic capability knowledge** comes from the health announce stream: the planner
  reads the digital twin (capability matrix + VRAM) as part of its prompt context — compact,
  one line per node.

### Data Model: Capability Matrix (reported by the nodes, continuously up to date)
```json
{
  "node": "node-a",
  "gpu": {"vram_free_mb": 12000, "vram_total_mb": 16384},
  "models": {
    "qwen2.5:7b":       {"loaded": true,  "vram_mb": 5100, "load_s": 6.2,  "tok_s": 14.1, "roles": ["planner"]},
    "qwen2.5-coder:32b":{"loaded": false, "vram_mb": 19800, "load_s": 95.0, "tok_s": 2.4, "roles": ["coder"], "cpu_offload": true},
    "gemma-31b":        {"loaded": false, "vram_mb": 14400, "load_s": 40.0, "tok_s": 5.0, "roles": ["critic"]}
  },
  "queue_depth": 0,
  "last_seen": "<ISO-8601 timestamp>"
}
```
- `loaded`/`vram_free` change **continuously** → health announce every 5–10 s.
- `load_s`/`tok_s` are measured during loading (Mesh-LLM `benchmark tune` idea) → the cost
  model grows with usage, with no manual maintenance effort.

### Flow per Task (K8s scheduler pattern, using pod info as the model)
1. **Request task**: the planner gets the task + task type (plan/coding/critique).
2. **Read twin**: the planner calls `mesh.resources.view` (request/reply on NATS) → gets the
   current capability matrix (K8s: `kubectl get pods -o wide`).
3. **Allocate**: the planner chooses `model × node` by these rules:
   - Does the model fit into free VRAM?
   - Does an already loaded model (load time 0) beat a faster model that still has to be loaded?
   - Load time + inference time vs. task deadline?
   - Supply alternatives as a `fallback` in case the node dies.
4. **Reserve**: the planner sends `mesh.alloc.request` → the orchestrator books the node
   (in-memory claim, analogous to K8s `nodeName` in the pod spec) and starts the worker.
   Conflict case (two planners at the same time): the first claim wins, the second gets
   `409` and reallocates.
5. **Execute + feedback**: after the task the node reports the measured `load_s`/`tok_s`
   back → the matrix updates (self-improving).

### Role Shift
- **Orchestrator**: manages claims, executes, broadcasts twin state — no routing knowledge
  needed anymore.
- **Planner**: the only component that decides about resources.
- **Model manager**: executes `load/unload/drain` intents (Mesh-LLM verbs).

### Milestones (adjusted)
- **MM1** — Model manager: health announce with the capability matrix (fields above).
- **MM2** — Orchestrator: collect twin state, `mesh.resources.view` endpoint
  (request/reply), log twin state visibly.
- **MM3** — Planner: mesh access (NATS user with `mesh.resources.view` permission),
  allocation as structured output, orchestrator executes.
- **MM4** — llauncher as serving/control layer (NATS↔MCP bridge) + `drain` intents:
  finally resolve the VRAM blockade chain.
- **MM5** — Cost model from measured load times (feedback loop).

## `memnc.control` + `meshctl backend`

The abstraction lives in memnc, not in the backend. llauncher and Ollama are adapters;
another backend is another module, without a core change.

```
src/memnc/control/
  base.py        Service, ActionResult, State/health vocabulary, BackendControl protocol
  llauncher.py   LlauncherControl — shells out to the llauncher CLI (one implementation for
                 "how to stop a unit" in the stack), maps JSON snapshots
  docker.py      DockerControl — containers as backend instances; qualification via the
                 memnc.backend label, docker_backends setting, or inference image
  llamacpp.py    LlamaCppControl — native llama-server processes directly from the host:
                 /proc/<pid>/cmdline + cgroup→systemd unit, without llauncher
  ollama.py      OllamaControl — models via HTTP (/api/tags, /api/ps); the process side
                 delegates to another adapter, otherwise systemctl
  __init__.py    controls() / find_control() / collect()
```

Design rules:

- **One vocabulary, no backend jargon.** Adapters translate to
  `running|stopped|failed|cloud|unknown` and `ok|down|open|bad|cloud`. Callers (CLI, later
  the planner) only switch on these values.
- **One owner per service.** `collect()` deduplicates via host:port
  (`http://127.0.0.1:11434` == `127.0.0.1:11434`) and merges the row: the process owner
  supplies the lifecycle, other adapters enrich the model inventory. That is why there is
  exactly one ollama row, even though two adapters know it.
- **Delegation instead of duplication.** `OllamaControl(delegates=[...])` does not control
  the process itself; it asks the adapter list for the endpoint (`/api/tags` host:port) and
  takes the first one that serves it — whether that is llauncher, docker or a native
  `llama-server`. If the delegate disappears, `systemctl` takes over the configured unit.
  `controls()` builds the list according to authority: llauncher → docker → llamacpp.
- **Host truth instead of registry truth.** The llamacpp adapter reads what is actually
  running (`/proc/<pid>/cmdline`, `--port`, `-m`) and resolves the owning systemd user unit
  via the process cgroup. Hand-started servers are thus visible and **stoppable**; a
  **restart** only works via a unit — memnc does not invent serving flags.
- **Containers only if they are backends.** The label `memnc.backend=…` is opt-in; after
  that the `docker_backends` setting (comma-separated names); finally known inference images.
  Mesh infrastructure (nats, workers) and third-party services (searxng, open-webui) stay out.
- **No NATS.** This is the emergency layer: when the mesh is down, `meshctl backend list`
  must still work.

CLI:

```
meshctl backend list [--backend llauncher] [--json]
meshctl backend status <name>
meshctl backend start|stop|restart <name>
meshctl backend logs <name> [-n 60]
```

Verification: 17 unit tests in `tests/test_backend_control.py` (mapping, dedupe, merge,
missing delegate, Ollama without daemon, Docker qualification/endpoint/labels,
llama-server cmdline + cgroup→unit), full suite 883 passed / 4 skipped.
Live: `list` shows `swift` :8097, `ollama` :11434 (compose `ollama_power`),
`hf-cheapest` (cloud, unmanaged). `start swift` is idempotent
(`ok — already_running swift`).

Open: `backend logs` for compose backends goes via the Docker adapter (`docker logs`); an
unmanaged `llama-server` only yields as much as journald or the unit provides — process
stdout outside systemd cannot be captured. `memnc.backend` labels on the real compose
services are still missing (that is why `ollama_power` currently does not appear via the
Docker adapter, only via llauncher).

## Open Questions (for the owner)

Tracked in roadmap E9 (T9.7–T9.11); answers go into the roadmap log.

- Should all nodes get llama-swap, or only the ones with a GPU (node-a: discrete GPU)?
- Keep Ollama as a backend behind llama-swap, or go directly to llama-server?
- Persistence strategy: which model should stay "always loaded"? (Suggestion: the smallest
  planner, ~5 GB — then the mesh task always remains startable.)
- Priority: first MM1+MM2 (observability), or directly MM3 (routing behavior)?
