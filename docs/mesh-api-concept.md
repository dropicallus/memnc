# Mesh API — a kubectl-style control plane for the memnc mesh

Status: concept (design only, not implemented). Feeds into plan.md as a future
milestone (working name: **M11 — Mesh API / Control Plane**).

## 1. Motivation

Today every interaction goes through a specific host: `meshctl` must run on the
node that hosts the orchestrator, Telegram talks to the Telegram observer, and
llauncher is controlled per-host. That breaks once the mesh runs on several
machines (`.124`, `.180`, …): "which node am I on?" is the wrong question.

kubectl analogy: one stable CLI verb set (`kubectl get/scale/logs`) that works
no matter where the cluster endpoints live. The mesh equivalent: one **Mesh
API** that any frontend (meshctl, Telegram, web UI, later a model/agent) can
talk to over NATS, and that routes to whichever node owns the state.

## 2. Command surface (v1)

Read-mostly plus a small set of state-changing verbs:

| Verb | Purpose | Maps to today |
|---|---|---|
| `task list` | tasks + state, filterable (state, node, role) | `meshctl state` |
| `task detail <id>` | snapshot, route, verdicts, events tail | `meshctl state`, `trace` |
| `task submit` | submit + fanout (write) | `meshctl submit` |
| `task cancel <id>` | cancel / requeue (write) | — (new) |
| `node list` | nodes, roles, presence, last heartbeat, model | `meshctl roster` |
| `node detail <node>` | role(s), backends, GPU, queue depth, reputation | — (new) |
| `node drain <node>` | stop claiming; graceful (write) | — (new) |
| `discuss <topic>` | ask the mesh a question (LLM discussion) | discuss driver |
| `listen` | stream `mesh.task.*.events` live | Telegram observer |
| `model list / model route get/set` | routing table, reputation | T4 route records |
| `llauncher start/stop/status` | backend lifecycle on a node | llancherer MCP |
| `grant / revoke` | ACL (admin only) | `meshctl grant` |

Design rule: the API is a thin routing layer — business logic stays in
orchestrator / workers / existing modules. The API server exposes, it does not
re-implement.

## 3. Topology: who hosts the endpoint

Three options, evaluated:

**A. Single master node (round-robin election).**
One node is elected master and hosts the API server. Others forward. Pros:
simple, one endpoint, easy to secure. Cons: master is a SPOF; needs
leader-election + failover.

**B. Full peer-to-peer.**
Every node serves the same API; requests go to any node via **NATS anycast**
(queue-group subscription), each node answers from the state it owns and
proxies the rest. Pros: no SPOF, latency-optimal. Cons: every node must
understand every request; consensus about cluster state is harder.

**C. Hybrid (recommended).**
- Stateless read commands (`node list`, `node detail`, `llauncher status`)
  are answered by **any node** via anycast queue-group — the responder either
  owns the data or proxies it.
- State-mutating commands (`task submit`, `grant`, `route set`, `drain`)
  are **any-cast claimed, then internally forwarded over the existing
  `mesh.task.*` / `mesh.ledger` subjects to the orchestrator** — the
  orchestrator remains the single writer (same guarantee as today, no new
  consensus protocol).
- The orchestrator still elects nothing: NATS queue-groups + a lightweight
  **presence-KV heartbeat** (already planned in M8) provide failover. If the
  API-answering node dies, another member of the queue group answers.
- `listen` becomes a JetStream **consumer** instead of a host-local
  subscription: durable, replayable, works from any node.

This is "P2P for reads, master-writer for writes" — the same shape as a
Kubernetes API server behind a load balancer, without running an election
protocol. If later we do want round-robin master election, it is an
optimization on top (a `mesh.api.master` KV key with CAS), not a redesign.

## 4. Wire protocol (additive)

```
mesh.api.request.<verb>      queue-group: mesh-api        (anycast request)
mesh.api.reply.<request_id>   request/reply inbox
mesh.api.events               JetStream (listen streams)
```

Envelope: same `MeshEnvelope` from docs/protocol.md §5.1, extended by
`request_id` and `reply_subject`. Every response carries `served_by` (node ID)
for debugging "which node answered".

Backward compatible: `meshctl` keeps working unchanged (it simply becomes the
first client of the new API).

## 5. Frontends

- **meshctl** — unchanged CLI, but now routed through the API (no longer
  requires being on the orchestrator host).
- **Telegram** — the observer becomes an API client; commands like
  `/tasks`, `/nodes`, `/discuss` hit `mesh.api.request.*`. Batching rules from
  PR #9 apply unchanged.
- **Web UI** — a small FastAPI proxy exposing the same verbs as REST/WS for a
  browser dashboard (read-only by default, writes gated by the ACL).
- **Model-mediated (later)** — an "API agent" receives natural-language
  requests, maps them to verbs (LLM tool-calling), executes, and answers. This
  is a frontend, not a new transport: same verbs, same ACL.

## 5a. Humans as peers — channels, presence, trace-watching

The API makes the mesh explorable like a workplace, not a dashboard. Two
additions to the command surface:

**Channels (teams).** A channel is a named group of workers plus its event
streams — `mesh.api.channel.<team>.*` subjects. Channel verbs (all over the
same API):

- `channel list` / `channel create / join / leave`
- `channel topic <team>` — what the team is currently working on
- `discuss <topic> --channel <team>` — start a debate inside a team
- `listen --channel <team>` — follow that team's events only

Frontends keep a *channel context*: Telegram `/channel <team>` switches which
team your messages go to; the web UI shows one channel per tab.

**Human as a participant.** The human is not a spectator — the existing
ask/WAIT_INPUT mechanism (agent asks the human instead of guessing) gets a
general form:

- The human joins a channel like a worker, with the same envelope and a
  `role: human` identity (ACL: human trust is set high but flagged).
- A human message in a channel is just an event other agents see — agents can
  address the human (`mesh.clarify`), and the human can reply mid-debate, so
  the discussion driver treats the human as one more turn.
- Participation modes: `observe` (read events/trace only), `join`
  (receive questions + can interject), `mediate` (the discuss-driver role —
  e.g. the daily review round with the human as mediator).

**Reasoning-trace following.** `trace follow <task>` streams the agent's
reasoning steps (subagent traces, verdicts, route decisions) as a live feed —
built on the same trace-proxy records, exposed through the API instead of a
local `/trace` console. Works from any frontend: Telegram gets step messages
(batched per PR #9), the web UI shows the live transcript.

Design note: this only adds channel addressing and a human identity to the
envelope — no new transport, no new consensus. A channel is a subject-namespace
convention, not new state.

## 6. Security

- Reuse the existing ACL (`acl.py`, `meshctl grant`): every request envelope
  carries the sender identity; M8's submitter-signed attribution (D3) signs
  mutating API calls.
- Verbs are classified `read` / `write` / `admin`; ACL roles map onto them.
  Telegram observer identity: read + a whitelist of writes.
- No new credentials: NATS token + existing identities.

## 7. Resource management (v2)

- `node detail` aggregates: heartbeat, presence, queue depth, model routes,
  GPU VRAM (from llancherer), reputation (T4).
- `node drain` + `worker scale` give the mesh basic self-management: drain
  before shutdown (as practiced manually with the .124 teardown), scale
  workers up when the queue grows.
- Optional guardrail: a warning when a node's task failure rate spikes
  (reputation already tracks this; the API only surfaces it).

## 8. Non-goals

- No scheduler/resource-bin-packing of *tasks* (the claim loop stays
  reputation/trust-driven). The API observes and controls backends, it does
  not place tasks by VRAM math.
- No new transport (no HTTP mesh-to-mesh, no gRPC): NATS is the bus.
- No cross-mesh federation in v1.

## 9. Rollout sketch

1. **M11a**: API server (anycast read path) + `node list/detail`, `listen`
   via JetStream consumer; meshctl routed through it. NATS-free unit tests
   with fakes, ruff/pytest green.
2. **M11b**: write path through the orchestrator (`submit/cancel/grant`),
   Telegram as API client.
3. **M11c**: web UI proxy + resource views (`llauncher status` in `node detail`).
4. **M11d**: model-mediated API agent (NL → verbs).

Decisions to record when implementing (SUMMARY file): election fallback
behavior, `served_by` semantics under proxying, rate limiting for `listen`.
