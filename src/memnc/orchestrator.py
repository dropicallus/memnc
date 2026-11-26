"""Orchestrator: durable state machine over NATS JetStream."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path

from . import acl, clarify, journal
from .acl import check_registry_write
from .api import ApiServer
from .bus import Bus
from .config import Settings, get_settings
from .failclass import MODEL_LEVEL_KINDS, classify_failure
from .fsm import Event, done_block_reason, transition
from .fsutil import apply_artifacts
from .harnesses.workspace import workspace_listing
from .observations import TaskObservation, observation_from_snapshot
from .pheromone import route_strategy, task_type_from_prompt
from .schema import (
    SUBJECT_ALLOC,
    SUBJECT_CMD,
    SUBJECT_CONTROL,
    SUBJECT_HEALTH_ALL,
    SUBJECT_HEARTBEAT,
    SUBJECT_LEDGER,
    SUBJECT_REQUEUE,
    SUBJECT_SUBMIT_NODE,
    TERMINAL_STATES,
    AllocResult,
    Background,
    CoderOutput,
    ControlCommand,
    CriticOutput,
    Envelope,
    GrantLevel,
    IntermediateOutput,
    Kind,
    MessageTuple,
    ModelEntry,
    NodeHealth,
    PlannerOutput,
    ResultPayload,
    Role,
    TaskSnapshot,
    TaskState,
    TaskSubmission,
    new_id,
    now_iso,
    plan_model_request,
)
from .scoring import router_candidates, score_candidates
from .version import code_version

log = logging.getLogger(__name__)


def now_iso_age_s(ts: str, now: float) -> float:
    """Age in seconds of an ISO-8601 timestamp (``Z`` allowed).

    Unparseable/empty timestamps return 0.0 so a malformed value is never
    mistaken for a stale claim (the reaper must be conservative).
    """
    try:
        return now - datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return 0.0


# M8: live router cadence + subject for published rankings.
ROUTER_INTERVAL = 10.0
ROUTER_SUBJECT = "mesh.router.scores"


# --- T5: planner allocation over the health twin -----------------------------


class Allocator:
    """Selects a node for a model from the live health twin.

    Selection order: (1) model already loaded → load time 0 wins,
    (2) most free VRAM that fits the (estimated) size. Claims are
    reservations per task: a second alloc for the same task is
    idempotent; capacity consumed by one claim is excluded for others.
    """

    def __init__(self, twin_stale_s: int = 30):
        self.twin_stale_s = twin_stale_s
        self._health: dict[str, NodeHealth] = {}
        self._seen_at: dict[str, float] = {}
        self._claims: dict[str, str] = {}  # task_id → node
        self._reserved_mb: dict[str, float] = {}  # node → reserved VRAM
        self._claim_size: dict[str, float] = {}  # task_id → reserved MB

    def update_health(self, *healths: NodeHealth) -> None:
        for health in healths:
            self._health[health.node] = health
            self._seen_at[health.node] = time.time()

    def _fresh(self) -> dict[str, NodeHealth]:
        now = time.time()
        fresh: dict[str, NodeHealth] = {}
        for node, h in self._health.items():
            if now - self._seen_at.get(node, 0.0) > self.twin_stale_s:
                continue
            try:  # announced ts must also be recent (staleness kills a node)
                age = now - datetime.fromisoformat(h.ts.replace("Z", "+00:00")).timestamp()
                if age > self.twin_stale_s:
                    continue
            except ValueError:
                pass
            fresh[node] = h
        return fresh

    def select(self, model: str, task_id: str, size_mb: float = 0.0) -> AllocResult:
        if task_id and task_id in self._claims:
            node = self._claims[task_id]
            return AllocResult(ok=True, model=model, node=node)

        fresh = self._fresh()
        if not fresh:
            return AllocResult(ok=False, model=model, reason="no_node")

        # (1) already loaded somewhere → zero load time wins.
        for node, health in sorted(
            fresh.items(), key=lambda kv: -kv[1].models.get(model, ModelEntry()).vram_mb
        ):
            entry = health.models.get(model)
            if entry is not None and entry.loaded:
                return self._claim(AllocResult(ok=True, model=model, node=node), task_id, 0.0)

        # (2) most free VRAM (minus reservations) that fits the size.
        candidates = []
        for node, health in fresh.items():
            free = health.gpu.vram_free_mb - self._reserved_mb.get(node, 0.0)
            if free >= max(size_mb, 0.0):
                candidates.append((free, node))
        if not candidates:
            return AllocResult(ok=False, model=model, reason="no_node")
        candidates.sort(reverse=True)
        free, node = candidates[0]
        return self._claim(AllocResult(ok=True, model=model, node=node), task_id, size_mb)

    def release(self, task_id: str) -> None:
        """Free a task's claim and VRAM reservation once the task is terminal.

        Without this, reservations only grew and each node eventually looked
        full to the planner.
        """
        node = self._claims.pop(task_id, "")
        size = self._claim_size.pop(task_id, 0.0)
        if node and size:
            left = self._reserved_mb.get(node, 0.0) - size
            if left > 0:
                self._reserved_mb[node] = left
            else:
                self._reserved_mb.pop(node, None)

    def _claim(
        self, result: AllocResult, task_id: str, size_mb: float
    ) -> AllocResult:
        if task_id:
            self._claims[task_id] = result.node or ""
        node = result.node or ""
        if node and size_mb:
            self._reserved_mb[node] = self._reserved_mb.get(node, 0.0) + size_mb
            if task_id:
                self._claim_size[task_id] = size_mb
        logging.getLogger(__name__).info(
            "alloc claim: task=%s model=%s node=%s size_mb=%.0f reserved=%.0f",
            task_id or "-",
            result.model,
            node,
            size_mb,
            self._reserved_mb.get(node, 0.0),
        )
        return result


class Orchestrator:
    def __init__(self, settings: Settings | None = None, bus: Bus | None = None):
        self.settings = settings or get_settings()
        self.bus = bus or Bus(self.settings)
        self._stop = asyncio.Event()
        self._results = None
        self._submit_sub = None
        self._heartbeat_sub = None
        # P5.1 lease by liveness: heartbeat time per worker identity
        # (node_name, e.g. "node-a-coder"), which is exactly the part of
        # `claimed_by` before the colon.
        self._last_seen: dict[str, float] = {}

    async def start(self) -> None:
        await self.bus.connect()
        self._router_task = asyncio.create_task(self._router_loop())
        Path(self.settings.workspace_root).mkdir(parents=True, exist_ok=True)
        assert self.bus.nc is not None
        self._submit_sub = await self.bus.nc.subscribe(
            # M15: wildcard — every orchestrator sees all submissions but only
            # handles those addressed to its own node (target = last token).
            SUBJECT_SUBMIT_NODE.format(node="*"),
            cb=self._on_submit,
        )
        # M10: queryable ledger — any granted agent can read the mesh state.
        self._ledger_sub = await self.bus.nc.subscribe(SUBJECT_LEDGER, cb=self._on_ledger)
        self._heartbeat_sub = await self.bus.nc.subscribe(
            SUBJECT_HEARTBEAT.format(node="*"), cb=self._on_heartbeat
        )
        # T2: human intervention commands (submit/cancel/retry/stop_worker)
        # from approved channels — grant-checked, then ledger-recorded.
        self._control_sub = await self.bus.nc.subscribe(SUBJECT_CONTROL, cb=self._on_control)
        # T5: model-management twin + planner allocation server.
        self._allocator = Allocator(twin_stale_s=self.settings.twin_stale_s)
        self._health_sub = await self.bus.nc.subscribe(SUBJECT_HEALTH_ALL, cb=self._on_health)
        self._alloc_sub = await self.bus.nc.subscribe(SUBJECT_ALLOC, cb=self._on_alloc)
        self._results = await self.bus.pull_results(
            durable=f"orchestrator-results-{self.settings.node_name}"
        )
        # M11a: anycast read API on mesh.api.request.* (queue-group mesh-api).
        self._api = ApiServer(self.settings, self.bus)
        await self._api.start()
        await self._recover()
        log.info("orchestrator ready", extra={"node": self.settings.node_name})
        await asyncio.gather(self._result_loop(), self._timeout_loop(), self._pheromone_loop())

    async def _pheromone_loop(self) -> None:
        """14.15.10: periodic pheromone evaporation (no-op when disabled)."""
        interval = self.settings.pheromone_decay_interval_s
        if interval <= 0:
            return
        while not self._stop.is_set():
            await asyncio.sleep(interval)
            try:
                updated = await self.bus.decay_pheromones(self.settings.pheromone_rho)
                if updated:
                    log.info("pheromone decay applied", extra={"cells": updated})
            except Exception:  # noqa: BLE001 - decay must not kill the loop
                log.exception("pheromone decay failed")

    async def stop(self) -> None:
        self._stop.set()

    async def aclose(self) -> None:
        if hasattr(self, "_router_task"):
            self._router_task.cancel()
        if getattr(self, "_api", None) is not None:
            await self._api.stop()
        self._stop.set()
        await self.bus.close()

    # -- submission --------------------------------------------------------
    def _owns(self, snapshot: TaskSnapshot) -> bool:
        """M15: only the origin node's orchestrator acts on a task.

        Snapshots created before M15 have an empty origin_node; those legacy
        tasks are handled only by the default orchestrator node.
        """
        # Snapshots created before M15 have an empty origin_node; those
        # legacy tasks stay visible to every orchestrator (claim-guard in
        # bus.try_claim already prevents double-claiming).
        return snapshot.origin_node in ("", self.settings.node_name)

    async def _on_submit(self, message) -> None:
        try:
            # M15: only the addressed node's orchestrator handles a
            # submission; the others stay silent (no double task creation).
            # Legacy: the default orchestrator (node_name "orchestrator")
            # also accepts "node-a" (the default NODE_NAME in .env).
            target_node = message.subject.split(".")[-1]
            accepted = {self.settings.node_name}
            if self.settings.node_name == "orchestrator":
                accepted.add("node-a")
            if target_node not in accepted:
                return
            submission = TaskSubmission.model_validate_json(message.data)
            snapshot = await self._accept_submission(submission)
            await message.respond(json.dumps({"task_id": snapshot.task_id}).encode())
        except Exception as exc:  # noqa: BLE001
            log.exception("submission failed")
            try:
                await message.respond(json.dumps({"error": str(exc)}).encode())
            except Exception:  # noqa: BLE001
                pass

    async def _accept_submission(
        self, submission: TaskSubmission, source: str = "meshctl"
    ) -> TaskSnapshot:
        """Create the snapshot and start the pipeline (shared by submit paths)."""
        # M11: recursion limit for agents-as-submitters.
        if submission.depth >= self.settings.max_submit_depth:
            raise ValueError(
                f"submission depth {submission.depth} >= "
                f"max_submit_depth {self.settings.max_submit_depth}"
            )
        snapshot = TaskSnapshot(
            task_id=new_id(),
            trace_id=new_id(),
            task=submission.task,
            context=submission.context,
            max_revisions=submission.max_revisions
            if submission.max_revisions is not None
            else self.settings.max_revisions,
            fanout=submission.fanout,
            depth=submission.depth,
            claimable=submission.claimable,
            needs=submission.needs,
            prefer_node=submission.prefer_node,
            harness=submission.harness,
            gate=submission.gate,
            # M14: worker-driven pipeline for claimable tasks
            next_role=("planner" if submission.claimable and self.settings.self_pipeline else ""),
            # M15: the orchestrator that accepted the submission owns it.
            origin_node=self.settings.node_name,
            state=TaskState.RECEIVED,
        )
        await self.bus.put_snapshot(snapshot)
        await self._save_background(snapshot)
        await self.bus.emit_event(snapshot, "task.received", f"[{source}] {submission.task[:180]}")
        log.info(
            "task submitted",
            extra={"task_id": snapshot.task_id, "task": submission.task[:200]},
        )
        if submission.claimable:
            # M11: referee mode — the orchestrator does NOT dispatch.
            # The task waits in RECEIVED until an agent CAS-claims it
            # (bus.try_claim); _timeout_loop then dispatches claimed ones.
            await self.bus.emit_event(snapshot, "task.claimable", "waiting for claim")
        else:
            await self._select_strategy(snapshot)
            await self._apply(snapshot, Event("start"))
        await self._maybe_request_model(snapshot)
        return snapshot

    # -- T5: health twin + planner allocation ---------------------------------

    async def _on_health(self, message) -> None:
        """Consume NodeHealth announces into the MODEL_MATRIX twin."""
        try:
            envelope = Envelope.model_validate_json(message.data)
            health = NodeHealth.model_validate(envelope.payload)
        except Exception:  # noqa: BLE001 - malformed health must not crash
            log.warning("discarding malformed health announce")
            return
        self._allocator.update_health(health)

    async def _on_alloc(self, message) -> None:
        """mesh.alloc.request: planner asks for a node for one model."""
        from .schema import AllocRequest

        try:
            request = AllocRequest.model_validate_json(message.data)
        except Exception:  # noqa: BLE001
            await message.respond(
                json.dumps({"ok": False, "model": "", "reason": "bad_request"}).encode()
            )
            return
        result = self._allocator.select(request.model, request.task_id)
        await message.respond(result.model_dump_json().encode())

    # -- T2: human intervention over approved channels -----------------------

    async def _on_control(self, message) -> None:
        """Grant-checked human commands: submit / cancel / retry / stop_worker /
        decide / pause_node / resume_node.

        Deny-by-default: the command's agent_id must hold a valid CHAIN grant
        (the Telegram observer gets one via ``meshctl grant`` once). Every
        accepted action is recorded on the task snapshot as a human.* event.
        """
        try:
            cmd = ControlCommand.model_validate_json(message.data)
            grant = await self.bus.grant_read(cmd.agent_id)
            decision = check_registry_write(grant, cmd.agent_id)
            if not decision:
                await message.respond(json.dumps({"error": decision.reason}).encode())
                return
            if cmd.action == "submit":
                if not cmd.task.strip():
                    raise ValueError("submit needs a task")
                snapshot = await self._accept_submission(
                    TaskSubmission(
                        task=cmd.task.strip(),
                        gate=cmd.gate_value() if cmd.gate else "never",
                    ),
                    source=f"human:{cmd.agent_id}",
                )
                snapshot.record("human.submit", f"by={cmd.agent_id} via control")
                await self.bus.put_snapshot(snapshot)
                await message.respond(
                    json.dumps({"ok": True, "task_id": snapshot.task_id}).encode()
                )
            elif cmd.action == "cancel":
                snapshot = await self._human_task_op(cmd)
                await self._apply(snapshot, Event("cancel", reason=f"by {cmd.agent_id}"))
                await message.respond(json.dumps({"ok": True}).encode())
            elif cmd.action == "retry":
                snapshot = await self._human_task_op(cmd)
                if snapshot.state not in (TaskState.FAILED, TaskState.CANCELLED):
                    raise ValueError(
                        f"retry needs a FAILED/CANCELLED task, {snapshot.task_id} is {snapshot.state.value}"
                    )
                # Retry-with-intent: a non-empty --model override reroutes every
                # re-dispatch of this snapshot; a plain retry keeps the old value.
                if cmd.model_override.strip():
                    snapshot.model_override = cmd.model_override.strip()
                snapshot.state = TaskState.RECEIVED
                snapshot.record("human.retry", f"by={cmd.agent_id}")
                await self.bus.put_snapshot(snapshot)
                await self.bus.emit_event(
                    snapshot, "human.retry", f"re-dispatched by {cmd.agent_id}"
                )
                await self._apply(snapshot, Event("start"))
                await self._maybe_request_model(snapshot)
                await message.respond(json.dumps({"ok": True}).encode())
            elif cmd.action == "decide":
                # T3 approval gate: approve/reject a parked task.
                snapshot = await self._human_task_op(cmd)
                if not snapshot.needs_human:
                    raise ValueError(
                        f"task {snapshot.task_id} is not parked ({snapshot.state.value})"
                    )
                if cmd.approved is None:
                    raise ValueError("decide needs approved true/false")
                snapshot.needs_human = False
                await self.bus.put_snapshot(snapshot)
                verdict = "approved" if cmd.approved else "rejected"
                await self.bus.emit_event(
                    snapshot, "human.decision", f"{verdict} by {cmd.agent_id}"
                )
                await self._apply(snapshot, Event("human_decision", approved=cmd.approved))
                await message.respond(
                    json.dumps(
                        {
                            "ok": True,
                            "task_id": snapshot.task_id,
                            "verdict": verdict,
                            "state": snapshot.state.value,
                        }
                    ).encode()
                )
            elif cmd.action == "answer":
                snapshot = await self._human_task_op(cmd)
                if not cmd.text:
                    raise ValueError("answer requires text")
                if snapshot.state is not TaskState.WAIT_INPUT:
                    raise ValueError(
                        f"task {snapshot.task_id} is not waiting for input ({snapshot.state.value})"
                    )
                # Feed the answer back into the prompt the agent will see on
                # re-dispatch — otherwise it would just ask again.
                snapshot.task = (
                    f"{snapshot.task}\n\nHuman answered the question "
                    f'"{snapshot.question}" with: {cmd.text}'
                )
                snapshot.question = ""
                snapshot.record("human.answered", f"by={cmd.agent_id}: {cmd.text}")
                await self.bus.put_snapshot(snapshot)
                await self.bus.emit_event(snapshot, "human.answered", f"answered by {cmd.agent_id}")
                await self._apply(
                    snapshot,
                    Event("answer", reason=cmd.text, role=snapshot.question_role),
                )
                await message.respond(
                    json.dumps(
                        {
                            "ok": True,
                            "task_id": snapshot.task_id,
                            "state": snapshot.state.value,
                        }
                    ).encode()
                )
            elif cmd.action in ("pause_node", "resume_node"):
                # T4: human pause — the node's claim loop stops pulling work
                # until resume (heartbeat refreshes keep the flag).
                if not cmd.node.strip():
                    raise ValueError(f"{cmd.action} needs a node name")
                node = await self.bus.set_node_paused(cmd.node.strip(), cmd.action == "pause_node")
                if node is None:
                    raise ValueError(f"unknown node {cmd.node.strip()}")
                log.info(
                    "node %s by human",
                    "paused" if cmd.action == "pause_node" else "resumed",
                    extra={"node": cmd.node, "by": cmd.agent_id},
                )
                await message.respond(
                    json.dumps({"ok": True, "node": node.node_id, "paused": node.paused}).encode()
                )
            elif cmd.action == "stop_worker":
                if not cmd.node.strip():
                    raise ValueError("stop_worker needs a node name")
                # Withdraw every capability record on that node so it receives
                # no further work; the process itself keeps running until the
                # operator stops it on the host (plan §14.6 T2 scope note).
                caps = await self.bus.list_capabilities()
                withdrawn = [cap.agent_id for cap in caps if cap.node == cmd.node.strip()]
                for agent_id in sorted(set(withdrawn)):
                    await self.bus.withdraw_capability(agent_id)
                log.info(
                    "worker stopped by human",
                    extra={"node": cmd.node, "by": cmd.agent_id, "agents": withdrawn},
                )
                await message.respond(json.dumps({"ok": True}).encode())
        except Exception as exc:  # noqa: BLE001
            log.exception("control command failed")
            try:
                await message.respond(json.dumps({"error": str(exc)}).encode())
            except Exception:  # noqa: BLE001
                pass

    async def _human_task_op(self, cmd: ControlCommand) -> TaskSnapshot:
        """Fetch the target task and record the human action on it."""
        if not cmd.task_id.strip():
            raise ValueError(f"{cmd.action} needs a task_id")
        snapshot = await self.bus.get_snapshot(cmd.task_id.strip())
        if snapshot is None:
            raise ValueError(f"unknown task {cmd.task_id.strip()}")
        snapshot.record(f"human.{cmd.action}", f"by={cmd.agent_id}")
        await self.bus.put_snapshot(snapshot)
        return snapshot

    # -- T7.5: model demand planning (plan §14.7) ----------------------------
    async def _maybe_request_model(self, snapshot: TaskSnapshot) -> None:
        """Raise a human-gated ModelRequest when needs.model is unmet everywhere.

        Loaded nowhere → "load" if some node has it on disk, else "pull" (T7).
        One pending request per model, ever — no request spam on resubmits.
        Best effort: a failure here must never break submission.
        """
        try:
            request = plan_model_request(
                snapshot.needs, await self.bus.list_nodes(), snapshot.task_id
            )
            if request is None:
                return
            existing = await self.bus.list_model_requests()
            if any(r.model == request.model and r.status == "pending" for r in existing):
                log.info("model request for %s already pending", request.model)
                return
            await self.bus.put_model_request(request)
            detail = (
                f"model {request.model} not loaded anywhere; "
                f"on disk at {', '.join(request.nodes_disk)}"
                if request.action == "load"
                else f"model {request.model} nowhere loaded or on disk; needs pull"
            )
            await self.bus.emit_event(snapshot, "model.request", detail)
            log.info(
                "model request %s raised (%s)",
                request.request_id,
                detail,
                extra={"task_id": snapshot.task_id},
            )
        except Exception:  # noqa: BLE001 - model planning is best effort
            log.debug("model request planning failed", exc_info=True)

    # -- M10: queryable ledger ----------------------------------------------
    async def _on_ledger(self, message) -> None:
        """Answer mesh.ledger requests with the consolidated mesh state."""
        try:
            await message.respond(json.dumps(await self.bus.read_ledger()).encode())
        except Exception:  # noqa: BLE001 - ledger read is best effort
            log.exception("ledger read failed")
            try:
                await message.respond(json.dumps({"error": "ledger read failed"}).encode())
            except Exception:  # noqa: BLE001
                pass

    # -- M9 grant bootstrap --------------------------------------------------
    def _agent_silence(self, claimed_by: str, now: float) -> float | None:
        """Seconds since the claiming worker's last heartbeat (None = unseen).

        ``claimed_by`` is ``<node_name>:<role>`` and workers beat under
        ``node_name``, so the part before the colon is the liveness key. A
        worker we have never seen beat returns None: the caller falls back to
        the conservative claim TTL rather than declaring a live worker dead.
        """
        node = claimed_by.partition(":")[0]
        last = self._last_seen.get(node)
        if last is None:
            return None
        return max(0.0, now - last)

    async def _on_heartbeat(self, message) -> None:
        """Issue CHAIN grants for agents of allowed joining nodes.

        Bootstrap for multi-machine meshes: a node listed in
        ``MESH_GRANT_NODES`` gets its agents granted on first heartbeat, so a
        new machine can join without a manual ``meshctl grant``. Revoked
        agents stay revoked — an existing grant record is never overwritten.
        """
        try:
            payload = Envelope.model_validate_json(message.data).payload or {}
            # P5.1: liveness bookkeeping happens before the grant bootstrap so
            # a heartbeat counts even when no grant pattern matches.
            node = payload.get("node") or ""
            if not node:
                node = str(message.subject).split(".")[2] if message.subject else ""
            if node:
                self._last_seen[node] = time.time()
            patterns = [p for p in self.settings.grant_nodes.split(",") if p.strip()]
            node = message.subject.split(".")[2] if message.subject.count(".") >= 3 else ""
            if not node or await self.bus.revocation_of(node) is not None:
                return
            for agent_id in acl.auto_grant_agents(node, payload, patterns):
                if await self.bus.grant_read(agent_id) is not None:
                    continue
                await self.bus.issue_grant(
                    agent_id,
                    level=GrantLevel.CHAIN,
                    granted_by=f"bootstrap:{self.settings.node_name}",
                )
                log.info("auto-granted %s (heartbeat bootstrap)", agent_id)
        except Exception:  # noqa: BLE001 - bootstrap is best effort
            log.debug("heartbeat bootstrap failed", exc_info=True)

    # -- results -----------------------------------------------------------
    async def _result_loop(self) -> None:
        while not self._stop.is_set():
            messages = await self.bus.fetch(self._results, batch=1, timeout_s=5)
            for message in messages:
                try:
                    await self._handle_result(message)
                except Exception:  # noqa: BLE001 - keep the loop alive
                    log.exception("result handling failed")
                    try:
                        await self.bus.dead_letter(
                            Envelope.model_validate_json(message.data),
                            "orchestrator error",
                        )
                    except Exception:  # noqa: BLE001
                        pass
                finally:
                    try:
                        await message.ack()
                    except Exception:  # noqa: BLE001
                        pass

    async def _handle_result(self, message) -> None:
        envelope = Envelope.model_validate_json(message.data)
        snapshot = await self.bus.get_snapshot(envelope.task_id)
        if snapshot is None:
            await self.bus.dead_letter(envelope, "unknown task")
            return
        if snapshot.state in TERMINAL_STATES:
            log.info("ignoring result for terminal task", extra={"task_id": snapshot.task_id})
            return
        if not self._owns(snapshot):  # M15: another node's orchestrator owns it
            return
        if snapshot.needs_human:  # T3: parked at the approval gate
            log.info("ignoring result for parked task", extra={"task_id": snapshot.task_id})
            return
        # M16 mesh-clarify: consult results from peers feed the clarify round
        # and must not pass through the normal result handling.
        if envelope.payload and (envelope.payload.get("output") or {}).get("consult"):
            consult = envelope.payload["output"]["consult"]
            await clarify.handle_consult(self.bus, snapshot, envelope, consult)
            return
        if envelope.id in snapshot.seen_result_ids:
            return
        # M14: worker-driven pipeline — the claiming worker advances the
        # snapshot itself; do not double-handle its results.
        if snapshot.next_role:
            return

        last_action = snapshot.last_action or {}
        expected_role = last_action.get("role")
        if expected_role and envelope.role and envelope.role.value != expected_role:
            log.warning(
                "ignoring stale result",
                extra={
                    "task_id": snapshot.task_id,
                    "expected": expected_role,
                    "got": envelope.role.value,
                },
            )
            return
        if (
            envelope.correlation_id
            and last_action.get("command_id")
            and envelope.correlation_id != last_action["command_id"]
        ):
            log.info(
                "ignoring result for superseded command",
                extra={"task_id": snapshot.task_id},
            )
            return

        try:
            payload = ResultPayload.model_validate(envelope.payload)
        except Exception as exc:  # noqa: BLE001
            log.warning("bad result payload: %s", exc)
            return
        # 15.4.5 gap 3 + ship-gate V2: the orchestrator has no filesystem
        # access, so the worker stamps claimed-vs-existing sizes and line
        # counts. Missing artifacts, thin stub files and empty deliveries
        # fail the step mechanically instead of passing on the narrative.
        if envelope.role is Role.CODER:
            sizes = payload.artifact_sizes or {}
            lines = payload.artifact_lines or {}
            missing = [p_ for p_, v_ in sizes.items() if v_ < 0]
            # A worker that does not stamp artifact_lines yet counts as 0
            # lines per claimed file: an old worker cannot smuggle stubs
            # past the gate while the rollout is in flight.
            thin = [
                f"{p_} ({lines.get(p_, 0)} lines < {self.settings.verify_min_lines})"
                for p_ in sizes
                if sizes[p_] >= 0 and lines.get(p_, 0) < self.settings.verify_min_lines
            ]
            too_few = (
                self.settings.verify_min_files > 0 and len(sizes) < self.settings.verify_min_files
            )
            if missing or thin or too_few:
                reasons = []
                if missing:
                    reasons.append(
                        "claimed artifacts missing from workspace: " + ", ".join(missing)
                    )
                if thin:
                    reasons.append("deliverables below verify_min_lines: " + ", ".join(thin))
                if too_few:
                    reasons.append(
                        f"only {len(sizes)} artifacts claimed, verify_min_files="
                        f"{self.settings.verify_min_files}"
                    )
                payload = ResultPayload(
                    ok=False,
                    error="; ".join(reasons),
                    tool_steps=payload.tool_steps,
                )

        snapshot.seen_result_ids.append(envelope.id)
        snapshot.seen_result_ids = snapshot.seen_result_ids[-50:]
        # T4 (model-routing-visible): record which node:model ran this step.
        if envelope.role is not None and payload.model:
            snapshot.route[envelope.role.value] = f"{envelope.node or '?'}:{payload.model}"

        event = Event("result", ok=payload.ok, reason=payload.error or "")
        if payload.ok and envelope.role is not None:
            try:
                self._store_output(snapshot, envelope.role, payload.output)
            except Exception as exc:  # noqa: BLE001
                event = Event("result", ok=False, reason=f"invalid output: {exc}")
            # An agent can ask a clarifying question instead of producing
            # output: park the task until a human answers it.
            if event.ok and snapshot.question_output(envelope.role):
                question = snapshot.question_output(envelope.role)
                event = Event("ask", reason=question, role=envelope.role)
            if (
                envelope.role is Role.CRITIC
                and snapshot.critique is not None
                and not snapshot.critique.question
            ):
                event = Event(
                    "result",
                    ok=True,
                    approved=snapshot.critique.approved,
                )
            # M10 hive fan-out: a multi-step plan on a fanout task is split
            # into independent subtasks; the parent waits and aggregates.
            if (
                event.ok
                and envelope.role is Role.PLANNER
                and snapshot.fanout
                and snapshot.plan is not None
                and len(snapshot.plan.plan) >= 2
            ):
                await self._fanout(snapshot)
                return
            if event.ok and event.type == "result":
                # T5: stream the coder/critic debate as events + history so
                # the negotiation is readable live (Telegram observer) and
                # replayable via /trace or `meshctl trace`.
                await self._emit_debate(snapshot, envelope.role)

        log.info(
            "result received",
            extra={
                "task_id": snapshot.task_id,
                "role": envelope.role.value if envelope.role else None,
                "ok": event.ok,
                "approved": event.approved,
                "executed_on": envelope.node,
            },
        )
        if not event.ok and envelope.role is not None:
            # 15.4.3 error-aware retry: rotate the failed role to another
            # advertised model on a model-level failure, BEFORE _apply
            # re-dispatches the retry.
            await self._maybe_rotate_model(snapshot, envelope.role, payload, event.reason)
        await self._apply(snapshot, event)
        if event.type == "ask":  # M16 mesh-clarify: consult peers first
            await clarify.start(self.bus, self.settings, snapshot)
        if envelope.node:
            # visibility for meshctl tail: which node executed this stage
            await self.bus.emit_event(snapshot, "result.executed_on", envelope.node)

    async def _emit_debate(self, snapshot: TaskSnapshot, role: Role) -> None:
        """T5: delegate the debate stream to the shared bus helper."""
        from .bus import emit_debate

        await emit_debate(self.bus, snapshot, role)

    def _store_output(self, snapshot: TaskSnapshot, role: Role, output: dict) -> None:
        if role is Role.PLANNER:
            snapshot.plan = PlannerOutput.model_validate(output)
        elif role is Role.CODER:
            snapshot.code = CoderOutput.model_validate(output)
            if self.settings.apply_artifacts:
                written = apply_artifacts(
                    Path(self.settings.workspace_root),
                    snapshot.code.files,
                    self.settings.max_artifact_bytes,
                )
                log.info(
                    "artifacts applied",
                    extra={"task_id": snapshot.task_id, "files": written},
                )
        elif role is Role.CRITIC:
            snapshot.critique = CriticOutput.model_validate(output)

    # -- structured message tuples (docs/roadmap.md) ----------
    def _background(self, snapshot: TaskSnapshot) -> Background:
        return Background(
            task=snapshot.task,
            context=snapshot.context,
            plan=snapshot.plan,
        )

    async def _save_background(self, snapshot: TaskSnapshot) -> None:
        """Persist the shared Background next to the snapshot. Best-effort and
        duck-typed like the observation hook: a KV hiccup or a fake bus without
        the method must never break the pipeline (tuples are an add-on)."""
        save = getattr(self.bus, "save_background", None)
        if save is None:
            return
        try:
            await save(snapshot.task_id, self._background(snapshot))
        except Exception as exc:  # observers keep broad excepts
            log.warning(
                "background persist failed",
                extra={"task_id": snapshot.task_id, "error": str(exc)},
            )

    def _message_tuple(self, snapshot: TaskSnapshot, role: Role) -> MessageTuple:
        """Assemble the dispatch tuple: short imperative message, shared
        background, and the previous hop's intermediate output."""
        io: IntermediateOutput | None = None
        if role is Role.CRITIC and snapshot.code is not None:
            io = IntermediateOutput(
                role=Role.CODER,
                summary=snapshot.code.summary,
                files=list(snapshot.code.files),
            )
        elif role is Role.CODER and snapshot.critique is not None:
            io = IntermediateOutput(
                role=Role.CRITIC,
                summary=snapshot.critique.summary,
                issues=list(snapshot.critique.issues),
            )
        messages = {
            Role.PLANNER: "Produce a plan for the task.",
            Role.CODER: "Implement the task.",
            Role.CRITIC: "Review the implementation.",
        }
        return MessageTuple(
            message=messages[role],
            background=self._background(snapshot),
            intermediate_output=io,
        )

    async def _suggest_model(self, snapshot: TaskSnapshot, role: str, error_text: str) -> None:
        """P2 item 9: suggest a known-good alternative on the failure card.

        When a task fails terminally on a model-level failure, look up the
        live roster for other models advertised for the failing role, skip
        retired ones, rank candidates by reputation approvals, and record the
        best pick as a ``model.suggest`` history entry (the Telegram failure
        card surfaces it). Advisory only: routing is never touched here."""
        kind = classify_failure(error_text)
        if kind not in MODEL_LEVEL_KINDS or not role:
            return
        route = snapshot.route or {}
        failed = str(route.get(role, "")).split(":")[-1]
        try:
            nodes = await self.bus.list_nodes()
            reps = await self.bus.list_reputation()
        except Exception:  # noqa: BLE001 - suggestion is best effort
            return
        rep = {r.model: r for r in reps}
        candidates: list[str] = []
        for node in nodes:
            if node.paused or role not in node.roles:
                continue
            for model in node.models:
                info = rep.get(model)
                if model == failed or model in candidates or (info is not None and info.retired):
                    continue
                candidates.append(model)
        if not candidates:
            snapshot.record("model.suggest", f"{role}: none (was {failed or 'unknown'}, {kind})")
            return
        candidates.sort(key=lambda m: rep[m].approvals if m in rep else 0, reverse=True)
        pick = candidates[0]
        snapshot.record("model.suggest", f"{role}: {pick} ({kind})")
        log.info(
            "model suggestion recorded",
            extra={"task_id": snapshot.task_id, "role": role, "failed": failed, "suggest": pick},
        )

    # -- transitions -------------------------------------------------------
    async def _maybe_rotate_model(
        self,
        snapshot: TaskSnapshot,
        role: Role,
        payload: ResultPayload,
        error_reason: str,
    ) -> None:
        """15.4.3 error-aware retry: rotate a failed role's model.

        A model-level failure (timeout / 5xx / invalid output) must not
        re-pick the same broken model. Picks the first other advertised
        model for the role from the live roster and stamps it into
        ``snapshot.model_overrides[role]``; the dispatch payload prefers it
        over the task-wide override. Best-effort: roster unavailability or
        no alternative simply keeps the normal retry path.
        """
        if not self.settings.model_rotate_on_fail:
            return
        if snapshot.attempt >= self.settings.max_attempts:
            return  # no retry will happen
        kind = classify_failure(error_reason or payload.error)
        if kind not in MODEL_LEVEL_KINDS:
            return
        existing = snapshot.model_overrides.get(role.value) or snapshot.model_override
        if existing:
            return  # a human/intent override wins; never second-guess it
        failed = payload.model or (snapshot.route.get(role.value, "").split(":")[-1] or "")
        try:
            nodes = await self.bus.list_nodes()
        except Exception:  # noqa: BLE001 - rotation is best effort
            return
        candidates: list[str] = []
        for node in nodes:
            if node.paused or role.value not in node.roles:
                continue
            for model in node.models:
                if model != failed and model not in candidates:
                    candidates.append(model)
        if not candidates:
            snapshot.record(
                "model.rotate",
                f"no alternative for {role.value} (was {failed or 'unknown'}, {kind})",
            )
            return
        pick = candidates[0]
        snapshot.model_overrides[role.value] = pick
        snapshot.record(
            "model.rotate",
            f"{role.value}: {failed or 'unknown'} -> {pick} ({kind})",
        )
        log.info(
            "model rotated after %s failure",
            kind,
            extra={"task_id": snapshot.task_id, "role": role.value, "from": failed, "to": pick},
        )

    async def _apply(self, snapshot: TaskSnapshot, event: Event) -> None:
        result = transition(
            snapshot.state,
            event,
            attempt=snapshot.attempt,
            revisions=snapshot.revisions,
            max_attempts=self.settings.max_attempts,
            max_revisions=snapshot.max_revisions,
            gate=snapshot.gate,
        )

        # 15.4.3 attempt journal: record what this attempt completed before
        # the attempt counter advances below.
        journal.note_failure(snapshot, event, result, event.role)

        changed = (
            result.state is not snapshot.state
            or bool(result.actions)
            or result.attempt_delta
            or result.attempt_reset
            or result.revision_delta
        )
        if not changed:
            return

        snapshot.state = result.state
        snapshot.attempt = 0 if result.attempt_reset else snapshot.attempt + result.attempt_delta
        snapshot.revisions += result.revision_delta
        snapshot.record(event.type, event.reason)
        if event.type in ("result", "failure", "timeout") and not event.ok:
            snapshot.error = event.reason or snapshot.error
        elif event.type == "result" and event.ok:
            snapshot.error = None
        if event.reason and result.state is TaskState.FAILED:
            snapshot.error = event.reason

        if result.state is TaskState.FAILED and event.reason:
            # P2 item 9: attach the alternative-model suggestion to the
            # snapshot BEFORE it is stored, so the failure card carries it.
            await self._suggest_model(
                snapshot,
                event.role.value
                if event.role
                else (list(snapshot.route or {})[-1] if snapshot.route else ""),
                event.reason,
            )

        await self.bus.put_snapshot(snapshot)
        await self.bus.emit_event(
            snapshot,
            f"state.{result.state.value.lower()}",
            event.reason,
        )
        log.info(
            "transition",
            extra={
                "task_id": snapshot.task_id,
                "state": result.state.value,
                "event": event.type,
                "revisions": snapshot.revisions,
                "attempt": snapshot.attempt,
            },
        )

        # 15.4.5: the activity gate decides the outcome before anything is
        # deposited, so a DONE it refuses is recorded as FAILED everywhere.
        gate_block = ""
        if self.settings.done_requires_activity and any(a.type == "finish" for a in result.actions):
            gate_block = done_block_reason(snapshot) or ""
        if gate_block:
            snapshot.state = TaskState.FAILED
            snapshot.record("task.failed", gate_block)
            snapshot.error = gate_block

        # 14.13 step 1: deposit an observation for every terminal transition
        # into the experience graph. Duck-typed + best effort: a broken
        # observation store must never fail the task lifecycle itself.
        if result.state in TERMINAL_STATES and getattr(self, "_allocator", None) is not None:
            self._allocator.release(snapshot.task_id)

        if result.state in TERMINAL_STATES and hasattr(self.bus, "deposit_observation"):
            await self._deposit_observation(snapshot)

        if result.state in TERMINAL_STATES and hasattr(self.bus, "deposit_pheromone"):
            await self._deposit_pheromone(snapshot)

        for action in result.actions:
            if action.type == "dispatch" and action.role is not None:
                await self._dispatch(snapshot, action.role)
            elif action.type == "finish":
                if gate_block:
                    await self.bus.put_snapshot(snapshot)
                    await self.bus.emit_event(snapshot, "task.failed", gate_block)
                    continue
                snapshot.record("task.done")
                await self.bus.put_snapshot(snapshot)
                await self.bus.emit_event(snapshot, "task.done", "workflow complete")
            elif action.type == "escalate":
                snapshot.record("task.failed", action.reason)
                await self.bus.put_snapshot(snapshot)
                await self.bus.emit_event(snapshot, "task.failed", action.reason)
                await self.bus.dead_letter(
                    Envelope(
                        task_id=snapshot.task_id,
                        trace_id=snapshot.trace_id,
                        kind="event",
                        node=self.settings.node_name,
                        payload={"reason": action.reason, "task": snapshot.task},
                    ),
                    action.reason,
                )
            elif action.type == "cancel":
                snapshot.record("task.cancelled", action.reason)
                await self.bus.put_snapshot(snapshot)
                await self.bus.emit_event(snapshot, "task.cancelled", action.reason)
            elif action.type == "park":
                # T3 approval gate: park at CRITIQUE for a human decision.
                snapshot.needs_human = True
                snapshot.record("task.parked", action.reason)
                await self.bus.put_snapshot(snapshot)
                verdict = "approved" if event.approved else "rejected"
                summary = snapshot.critique.summary if snapshot.critique else ""
                await self.bus.emit_event(
                    snapshot,
                    "task.parked",
                    f"critic {verdict}; {action.reason} — /approve or /reject "
                    f"{snapshot.task_id} | {summary}",
                )

            elif action.type == "ask":
                # Agent asked a clarifying question: surface it to the human.
                snapshot.question = action.reason
                snapshot.question_role = action.role
                snapshot.record("task.question", action.reason)
                await self.bus.put_snapshot(snapshot)
                await self.bus.emit_event(
                    snapshot,
                    "task.question",
                    f"question: {action.reason} — /answer {snapshot.task_id} <text>",
                )

        if result.state is TaskState.REVISE:
            # 14.15.10: a failed verify is a failed edge - re-select so
            # pheromone never re-picks the strategy that just failed.
            await self._select_strategy(snapshot, failed_edge=snapshot.routing_edge)
            await self._apply(snapshot, Event("start"))

    async def _select_strategy(self, snapshot: TaskSnapshot, failed_edge: str = "") -> None:
        """14.15.10 selection consumption: pick the strategy edge.

        Reads the live pheromone field and runs route_strategy under the
        escalation floor: pheromone may only raise the choice above the
        floor, never re-pick the edge that just failed. Best effort — an
        unavailable bucket leaves the default chain head in place.
        """
        if not self.settings.pheromone_select:
            return
        try:
            entries = await self.bus.pheromone_entries()
        except Exception:  # noqa: BLE001 - selection is best effort
            return
        edge = route_strategy(
            entries,
            task_type_from_prompt(snapshot.task),
            failed_edge=failed_edge or None,
            weak_planner=self.settings.pheromone_weak_planner,
        )
        if edge == snapshot.routing_edge:
            return
        snapshot.routing_edge = edge
        snapshot.record("route.strategy", edge)

    async def _deposit_pheromone(self, snapshot: TaskSnapshot) -> None:
        """14.15.10: fold the terminal outcome into the pheromone field.

        Live tasks run the fixed pipeline, so they deposit on the single
        LIVE_EDGE; the accumulated tau per task type is what later strategy
        selection reads. An unsure marker in the error or critique summary
        is folded as a weak "unsure" deposit on the self-reflect edge
        instead of a success/failure on LIVE_EDGE. Best effort, never
        raising, like observations.
        """
        from .pheromone import LIVE_EDGE, SELF_REFLECT_EDGE, task_type_from_prompt, unsure_signal

        try:
            outcome = "success" if snapshot.state.value.lower() == "done" else "failure"
            # Selection consumption (14.15.10): deposit on the edge the task
            # actually ran; LIVE_EDGE only when selection never picked one.
            edge = getattr(snapshot, "routing_edge", "") or LIVE_EDGE
            critique = getattr(snapshot, "critique", None)
            critique_summary = critique.summary if critique else ""
            if unsure_signal(getattr(snapshot, "error", None), critique_summary):
                outcome = "unsure"
                edge = SELF_REFLECT_EDGE
            await self.bus.deposit_pheromone(task_type_from_prompt(snapshot.task), edge, outcome)
        except Exception:  # noqa: BLE001 - side channel, never fail the task
            log.debug("pheromone deposit failed for %s", snapshot.task_id)

    async def _deposit_observation(self, snapshot: TaskSnapshot) -> None:
        """14.13 step 1: write the terminal snapshot into mesh.observations.

        Never raises: observation is a side channel; a KV failure here must
        not corrupt the already-applied task transition.
        """
        observation = observation_from_snapshot(snapshot)
        if observation is None:
            return
        try:
            await self.bus.deposit_observation(observation)
        except Exception as exc:  # noqa: BLE001 - side channel only
            log.warning(
                "observation deposit failed",
                extra={"task_id": snapshot.task_id, "error": str(exc)},
            )
            return
        if self.settings.experience_from_observations:
            try:
                self._record_experience(observation)
            except Exception as exc:  # noqa: BLE001 - side channel only
                log.warning(
                    "experience record failed",
                    extra={"task_id": snapshot.task_id, "error": str(exc)},
                )

    def _record_experience(self, observation: TaskObservation) -> None:
        """15.3 item 5: fold a terminal observation into the experience store.

        File-backed, never raises into the pipeline (caller catches). The
        store is a side channel: a write failure costs one bullet, nothing
        else.
        """
        from .experience_store import record_observation_locked

        record_observation_locked(self.settings.experience_store_path, observation)

    async def _dispatch(self, snapshot: TaskSnapshot, role: Role) -> None:
        timeout_s = self.settings.timeout_for(role)
        # T7: best-effort planner allocation — claim a node for the role
        # model via mesh.alloc.request and stamp it into the command.
        alloc: dict | None = None
        try:
            model = (
                snapshot.model_overrides.get(role.value)
                or snapshot.model_override
                or snapshot.needs.model
            )
            if model and snapshot.needs is not None and snapshot.needs.model:
                alloc = await self._alloc_for(model, snapshot.task_id)
        except Exception:  # noqa: BLE001 - allocation is best effort
            log.debug("alloc for %s failed", role.value, exc_info=True)
        command = Envelope(
            task_id=snapshot.task_id,
            trace_id=snapshot.trace_id,
            kind="command",
            role=role,
            node=self.settings.node_name,
            state=snapshot.state,
            attempt=snapshot.attempt,
            timeout_ms=timeout_s * 1000,
            payload={
                "task": snapshot.task,
                "context": snapshot.context,
                "plan": snapshot.plan.model_dump() if snapshot.plan else None,
                "code": snapshot.code.model_dump() if snapshot.code else None,
                "critique": snapshot.critique.model_dump() if snapshot.critique else None,
                "revisions": snapshot.revisions,
                "last_error": snapshot.error,
                "workspace_root": str(self.settings.workspace_root),
                # 15.4.3 workspace continuity: on retries, tell the role what
                # the shared workspace already contains (prior attempts may
                # have left usable state — a venv, binaries, artifacts).
                "workspace_state": (
                    workspace_listing(Path(self.settings.workspace_root))
                    if snapshot.attempt > 1
                    else None
                ),
                "role": role.value,
                "prefer_node": snapshot.prefer_node,
                "harness": snapshot.harness,
                # 15.4.3: a rotated role model (error-aware retry) wins over
                # the task-wide override for THIS role only.
                "model_override": (
                    snapshot.model_overrides.get(role.value) or snapshot.model_override
                ),
                # P4 slice 2: mesh code fingerprint — stale workers refuse.
                "mesh_version": code_version(),
                # 14.15.10: strategy edge chosen from the pheromone field
                # ("") = orchestrator never selected / selection disabled).
                "strategy": snapshot.routing_edge,
                # 15.4.3: compact per-attempt checkpoint notes.
                "attempt_journal": snapshot.attempt_journal,
                # T7: planner allocation claim ({"model", "node"} | None).
                "alloc": alloc,
            },
        )
        if self.settings.tuple_prompts:
            command.payload["message_tuple"] = self._message_tuple(snapshot, role).model_dump()
        snapshot.last_action = {
            "command_id": command.id,
            "role": role.value,
            "at": now_iso(),
            "at_epoch": time.time(),
            "timeout_s": timeout_s,
        }
        await self.bus.put_snapshot(snapshot)
        await self.bus.publish_envelope(SUBJECT_CMD.format(role=role.value), command)
        await self.bus.emit_event(snapshot, "dispatch", f"{role.value} command {command.id}")

    # -- T7: planner allocation client ---------------------------------------

    async def _alloc_for(self, model: str, task_id: str) -> dict | None:
        """Ask the allocator for a node for `model`; returns {model, node}."""
        assert self.bus.nc is not None
        response = await self.bus.nc.request(
            SUBJECT_ALLOC,
            json.dumps({"model": model, "task_id": task_id}).encode(),
            timeout=5,
        )
        result = json.loads(response.data) or {}
        if result.get("ok") and result.get("node"):
            return {"model": model, "node": result["node"]}
        return None

    # -- M10: fan-out (hive run_worker shape) -------------------------------
    async def _fanout(self, snapshot: TaskSnapshot) -> None:
        """Emit each plan step as an independent subtask on mesh.task.submit.

        Returns immediately after emission: subtasks run their own full
        pipeline (planner->coder->critic) on whichever node's workers claim
        them; the parent aggregates in ``_check_fanouts``.
        """
        assert self.bus.nc is not None
        assert snapshot.plan is not None  # caller guarantees this
        for step in snapshot.plan.plan:
            submission = TaskSubmission(
                task=step.description or f"step {step.id}",
                context=f"fanout parent={snapshot.task_id} step={step.id}: {snapshot.task[:120]}",
                depth=snapshot.depth + 1,
                claimable=snapshot.claimable,
            )
            try:
                response = await self.bus.nc.request(
                    SUBJECT_SUBMIT_NODE.format(
                        node=snapshot.origin_node or self.settings.node_name
                    ),
                    submission.model_dump_json().encode(),
                    timeout=10,
                )
                sub_id = (json.loads(response.data) or {}).get("task_id")
            except Exception:  # noqa: BLE001 - skip this step, keep fanning out
                log.exception("fanout submit failed for step %s", step.id)
                continue
            if not sub_id:
                continue
            snapshot.subtasks[sub_id] = step.id
            sub = await self.bus.get_snapshot(sub_id)
            if sub is not None:
                sub.parent_task_id = snapshot.task_id
                await self.bus.put_snapshot(sub)
        if not snapshot.subtasks:
            # nothing was fanned out — continue the normal single pipeline
            await self._apply(snapshot, Event("result", ok=True))
            return
        snapshot.self_assign = True  # parent parks in CODING, waits for subtasks
        # No at_epoch key: the timeout reaper skips fanout-wait parents; the
        # aggregation runs in _check_fanouts instead.
        snapshot.last_action = {"role": "fanout-wait"}
        snapshot.record("task.fanout", f"{len(snapshot.subtasks)} subtasks")
        await self.bus.put_snapshot(snapshot)
        await self.bus.emit_event(
            snapshot, "task.fanout", f"{len(snapshot.subtasks)} subtasks emitted"
        )
        log.info(
            "fanout",
            extra={"task_id": snapshot.task_id, "subtasks": len(snapshot.subtasks)},
        )

    async def _check_fanouts(self) -> None:
        """Aggregate fan-out parents whose subtasks have all gone terminal."""
        for snapshot in await self.bus.list_tasks():
            if not self._owns(snapshot):
                continue
            if snapshot.state in TERMINAL_STATES or not snapshot.subtasks:
                continue
            states: list[TaskState] = []
            for sub_id in snapshot.subtasks:
                sub = await self.bus.get_snapshot(sub_id)
                states.append(sub.state if sub is not None else TaskState.FAILED)
            if not all(s in TERMINAL_STATES for s in states):
                continue
            if any(s in (TaskState.FAILED, TaskState.CANCELLED) for s in states):
                reason = "fanout: a subtask failed or was cancelled"
                snapshot.state = TaskState.FAILED
                snapshot.error = reason
                snapshot.record("task.failed", reason)
                await self.bus.put_snapshot(snapshot)
                await self.bus.emit_event(snapshot, "task.failed", reason)
                await self.bus.dead_letter(
                    Envelope(
                        task_id=snapshot.task_id,
                        trace_id=snapshot.trace_id,
                        kind=Kind.EVENT,
                        node=self.settings.node_name,
                        payload={"reason": reason, "task": snapshot.task},
                    ),
                    reason,
                )
                log.warning("fanout failed", extra={"task_id": snapshot.task_id})
            else:
                snapshot.state = TaskState.DONE
                snapshot.record("task.done", "fanout complete")
                await self.bus.put_snapshot(snapshot)
                await self.bus.emit_event(snapshot, "task.done", "fanout complete")
                log.info("fanout done", extra={"task_id": snapshot.task_id})

    # -- timers / recovery -------------------------------------------------
    async def _timeout_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(5)
            try:
                await self._reap_timeouts()
                await self._reap_stale_claims()
                await self._check_fanouts()
                await self._dispatch_claimed()
            except Exception:  # noqa: BLE001
                log.exception("timeout reaper failed")

    async def _reap_timeouts(self) -> None:
        snapshots = await self.bus.list_tasks()
        now = time.time()
        for snapshot in snapshots:
            if not self._owns(snapshot):
                continue
            if snapshot.state in TERMINAL_STATES:
                continue
            last = snapshot.last_action
            if not last or "at_epoch" not in last:
                continue
            limit = float(last.get("timeout_s", 60)) + self.settings.orchestrator_grace_s
            if now - float(last["at_epoch"]) > limit:
                log.warning(
                    "step timed out",
                    extra={
                        "task_id": snapshot.task_id,
                        "role": last.get("role"),
                        "state": snapshot.state.value,
                    },
                )
                await self._apply(
                    snapshot,
                    Event(
                        "timeout",
                        ok=False,
                        reason=f"{last.get('role')} timed out after {int(limit)}s",
                    ),
                )

    async def _reap_stale_claims(self) -> None:
        """P5.1 lease by liveness: free CAS claims whose worker is gone.

        Two triggers:
        * liveness — the claiming node has been silent for
          ``heartbeat_stale_s`` (three missed beats): released at once, so a
          ``kill -9`` recovers in ~90 s instead of 15 min;
        * conservative fallback — the claim is older than ``claim_ttl_s``
          (a live but stuck worker).

        A node we have never seen beating falls back to the TTL path: no
        evidence of death, only of an old claim.
        """
        now = time.time()
        for snapshot in await self.bus.list_tasks():
            if not self._owns(snapshot):
                continue
            if snapshot.state in TERMINAL_STATES or not snapshot.claimed_by:
                continue
            if snapshot.claimed_at_epoch > 0:
                age = now - snapshot.claimed_at_epoch
            else:
                age = now_iso_age_s(snapshot.updated_at, now)
            silence = self._agent_silence(snapshot.claimed_by, now)
            dead = silence is not None and silence > self.settings.heartbeat_stale_s
            if not dead and age <= self.settings.claim_ttl_s:
                continue
            reason = (
                f"{snapshot.claimed_by} silent for {int(silence or 0)}s"
                if dead
                else f"claim from {snapshot.claimed_by} older than {int(age)}s"
            )
            reaped = await self.bus.release_claim(snapshot.task_id, reason)
            if reaped is None:
                continue
            log.warning(
                "stale claim reaped",
                extra={
                    "task_id": reaped.task_id,
                    "claimed_by": snapshot.claimed_by,
                    "age_s": int(age),
                    "silence_s": None if silence is None else int(silence),
                    "reaps": reaped.reaps,
                    "reason": reason,
                },
            )
            # One requeue announcement for anyone tailing the ledger, plus the
            # event stream entry the FSM-side consumers already read. Both are
            # notifications: if the stream does not cover the subject (older
            # deployment, stream not migrated yet) the release must still
            # stand and the other tasks in this pass must still be reaped.
            try:
                await self.bus.publish_envelope(
                    SUBJECT_REQUEUE.format(task_id=reaped.task_id),
                    Envelope(
                        task_id=reaped.task_id,
                        trace_id=reaped.trace_id,
                        kind=Kind.EVENT,
                        node=self.settings.node_name,
                        state=reaped.state,
                        attempt=reaped.attempt,
                        payload={
                            "reason": reason,
                            "role": reaped.next_role,
                            "reaps": reaped.reaps,
                            "reaped_from": snapshot.claimed_by,
                        },
                    ),
                )
                await self.bus.emit_event(reaped, "claim.reaped", reason)
            except Exception as exc:  # noqa: BLE001 - notification only
                log.warning(
                    "requeue announcement failed",
                    extra={"task_id": reaped.task_id, "error": str(exc)[:200]},
                )

    async def _dispatch_claimed(self) -> None:
        """M11 referee: dispatch claimable tasks that have been CAS-claimed.

        Agents claim via bus.try_claim (CAS on the KV entry). The orchestrator
        does not assign — it only validates a claim and starts the pipeline.
        """
        for snapshot in await self.bus.list_tasks():
            if not self._owns(snapshot):
                continue
            if (
                snapshot.state is TaskState.RECEIVED
                and snapshot.claimable
                and snapshot.claimed_by
                and not snapshot.last_action
                and not snapshot.next_role  # M14: worker-driven, not ours
            ):
                log.info(
                    "claim accepted",
                    extra={
                        "task_id": snapshot.task_id,
                        "claimed_by": snapshot.claimed_by,
                    },
                )
                # M8: attribution is written by bus.try_claim at CAS win.
                snapshot.record("task.claimed", snapshot.claimed_by)
                await self.bus.put_snapshot(snapshot)
                await self._apply(snapshot, Event("start"))

    async def _recover(self) -> None:
        for snapshot in await self.bus.list_tasks():
            if not self._owns(snapshot):
                continue
            if snapshot.state in TERMINAL_STATES:
                continue
            if snapshot.state is TaskState.REVISE:
                await self._apply(snapshot, Event("start"))
            elif (
                snapshot.state is TaskState.RECEIVED
                and not snapshot.last_action
                and not (snapshot.claimable and not snapshot.claimed_by)
            ):
                await self._apply(snapshot, Event("start"))
            else:
                log.info(
                    "recovered in-flight task",
                    extra={"task_id": snapshot.task_id, "state": snapshot.state.value},
                )

    async def _router_loop(self) -> None:
        """M8: live utility router over NATS.

        Every ROUTER_INTERVAL: build the Candidate list from the T6 roster
        (presence) + trust ledger, score it with the SwarmHarness scorer
        and publish the ranking on ROUTER_SUBJECT. LLM-free (DEALS-style):
        routing evidence comes from presence + trust metadata only.
        """

        while not self._stop.is_set():
            try:
                nodes = await self.bus.list_nodes()
                trust = {t.subject: t.credit for t in await self.bus.list_trust()}
                candidates = router_candidates(nodes, trust)
                ranking = score_candidates(candidates)
                if ranking:
                    await self.bus.nc.publish(
                        ROUTER_SUBJECT,
                        json.dumps(
                            {
                                "ts": now_iso(),
                                "scores": [{"agent": a, "score": round(s, 4)} for a, s in ranking],
                            }
                        ).encode(),
                    )
                    log.info(
                        "router ranking published",
                        extra={"top": ranking[0][0]},
                    )
            except Exception:  # noqa: BLE001 - the router must never die
                log.exception("router loop error")
            await asyncio.sleep(ROUTER_INTERVAL)


async def _main() -> None:
    import signal

    from .log import setup_logging

    settings = get_settings()
    setup_logging(settings.log_level)
    orchestrator = Orchestrator(settings)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(orchestrator.stop()))
        except NotImplementedError:  # pragma: no cover
            pass
    try:
        await orchestrator.start()
    finally:
        await orchestrator.aclose()


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
