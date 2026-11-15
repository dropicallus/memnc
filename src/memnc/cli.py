"""meshctl: submit tasks, tail events, inspect state, check connectivity."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import sys
import time
from datetime import UTC, datetime

import nats

from .backends import make_backend
from .backlog import (
    MAX_ATTEMPTS,
    BacklogStore,
    decompose_plan,
    make_strong_retry,
    should_strong_retry,
)
from .backup import dump_buckets, restore_buckets
from .bus import Bus
from .config import Settings, get_settings
from .schema import (
    SUBJECT_CONTROL,
    SUBJECT_EVENTS,
    SUBJECT_LEDGER,
    SUBJECT_SUBMIT_NODE,
    TERMINAL_STATES,
    ControlCommand,
    GrantLevel,
    TaskNeeds,
    TaskSubmission,
    model_pull_allowed,
)
from .version import code_version, is_stale
from .wol import WolWaker, send_wol


async def _connect(settings: Settings):
    # H4: NKey mode mirrors bus.py — the seed replaces the token and the
    # configured seed path must exist (fail closed, no token fallback).
    if settings.nats_nkeys_seed:
        seed_path = settings.nats_nkeys_seed
        if not os.path.isfile(seed_path):
            raise RuntimeError(
                f"NATS_NKEYS_SEED set to {seed_path!r} but the file does not "
                "exist — refusing to fall back to the shared token (fail closed)."
            )
        return await nats.connect(
            settings.nats_servers,
            nkeys_seed=seed_path,
            name=f"meshctl-{settings.node_name}",
            max_reconnect_attempts=2,
            connect_timeout=5,
        )
    return await nats.connect(
        settings.nats_servers,
        token=settings.nats_token or None,
        name=f"meshctl-{settings.node_name}",
        max_reconnect_attempts=2,
        connect_timeout=5,
    )


async def cmd_submit(args: argparse.Namespace, settings: Settings) -> int:
    client = await _connect(settings)
    try:
        submission = TaskSubmission(
            task=args.task,
            context=args.context or "",
            fanout=args.fanout,
            claimable=getattr(args, "claimable", False),
            # T6: optional capability constraints for the claim gate.
            needs=_needs_from_args(args),
            gate=args.gate,  # type: ignore[arg-type]
            harness=getattr(args, "harness", "") or "",
            prefer_node=getattr(args, "prefer", "") or "",
        )
        response = await client.request(
            # M15: owner-routed submission — default to this machine's node
            # name so any node's meshctl submits to its own orchestrator.
            SUBJECT_SUBMIT_NODE.format(node=getattr(args, "node", None) or settings.node_name),
            submission.model_dump_json().encode(),
            timeout=10,
        )
        data = json.loads(response.data)
        if "error" in data:
            print(f"error: {data['error']}", file=sys.stderr)
            return 1
        print(data["task_id"])
        if args.watch:
            await _watch(client, data["task_id"])
        return 0
    finally:
        await client.close()


_TERMINAL_EVENTS = {
    "task.done",
    "task.failed",
    "task.cancelled",
    # state.done is also final: the worker-driven pipeline only RECORDS
    # task.done without emitting it, so a successful debate ends in state.done.
    # state.failed must NOT be terminal -- it fires once per debate attempt
    # while the orchestrator keeps retrying; an exhausted task emits task.failed.
    "state.done",
}


def _needs_from_args(args: argparse.Namespace):
    """Build TaskNeeds from --needs-* flags; None when no flag is set."""
    vram = getattr(args, "needs_vram", 0.0) or 0.0
    model = getattr(args, "needs_model", "") or ""
    roles = getattr(args, "needs_role", None) or []
    if not vram and not model and not roles:
        return None
    return TaskNeeds(vram_gb=vram, model=model, roles=roles)


async def _watch(client, task_id: str) -> None:
    """Follow one task's event stream until a terminal event (submit --watch)."""
    subject = SUBJECT_EVENTS.format(task_id=task_id)
    subscription = await client.subscribe(subject)
    print(f"watching {subject} (ctrl-c to stop)", file=sys.stderr)
    try:
        while True:
            try:
                message = await subscription.next_msg(timeout=1.0)
            except Exception:
                continue
            try:
                envelope = json.loads(message.data)
            except json.JSONDecodeError:
                continue
            payload = envelope.get("payload") or {}
            event = payload.get("event", "")
            detail = payload.get("detail", "")
            print(f"{envelope.get('ts', '')} {event:<24} {detail}".rstrip())
            if event in _TERMINAL_EVENTS:
                return
    except KeyboardInterrupt:
        pass


async def cmd_tail(args: argparse.Namespace, settings: Settings) -> int:
    client = await _connect(settings)
    subject = SUBJECT_EVENTS.format(task_id=args.task_id)
    subscription = await client.subscribe(subject)
    print(f"tailing {subject} (ctrl-c to stop)", file=sys.stderr)
    seen = 0
    try:
        while args.limit is None or seen < args.limit:
            try:
                message = await subscription.next_msg(timeout=1.0)
            except Exception:
                continue
            try:
                envelope = json.loads(message.data)
            except json.JSONDecodeError:
                continue
            payload = envelope.get("payload") or {}
            state = payload.get("state") or envelope.get("state") or ""
            event = payload.get("event", "")
            detail = payload.get("detail", "")
            print(f"{envelope.get('ts', '')} {state:<9} {event:<24} {detail}".rstrip())
            seen += 1
    except KeyboardInterrupt:
        pass
    finally:
        await client.close()
    return 0


async def cmd_state(args: argparse.Namespace, settings: Settings) -> int:
    bus = Bus(settings)
    await bus.connect()
    try:
        snapshot = await bus.get_snapshot(args.task_id)
        if snapshot is None:
            print(f"task not found: {args.task_id}", file=sys.stderr)
            return 1
        print(snapshot.model_dump_json(indent=2))
        return 0
    finally:
        await bus.close()


async def cmd_trace(args: argparse.Namespace, settings: Settings) -> int:
    """T5: replay the coder/critic debate from the task snapshot history."""
    from .telegram import format_trace

    bus = Bus(settings)
    await bus.connect()
    try:
        snapshot = await bus.get_snapshot(args.task_id)
        if snapshot is None:
            print(f"task not found: {args.task_id}", file=sys.stderr)
            return 1
        print(format_trace(snapshot.model_dump()))
        return 0
    finally:
        await bus.close()


async def cmd_tasks(_: argparse.Namespace, settings: Settings) -> int:
    bus = Bus(settings)
    await bus.connect()
    try:
        snapshots = await bus.list_tasks()
        if not snapshots:
            print("no tasks")
            return 0
        for snapshot in sorted(snapshots, key=lambda s: s.created_at):
            terminal = "terminal" if snapshot.state in TERMINAL_STATES else "active"
            print(
                f"{snapshot.task_id}  {snapshot.state.value:<9} "
                f"rev={snapshot.revisions}  {terminal}  {snapshot.task[:60]}"
            )
        return 0
    finally:
        await bus.close()


async def cmd_exp(args: argparse.Namespace, settings: Settings) -> int:
    """15.3 item 5: read the automated experience store (no NATS connection)."""
    from memnc.experience_store import ExperienceStore, format_bullets

    store = ExperienceStore(settings.experience_store_path).load()
    print(format_bullets(store, args.topic, args.limit))
    return 0


async def cmd_obslast(args: argparse.Namespace, settings: Settings) -> int:
    """14.13 step 2: latest deposited observations, optional model filter."""
    from memnc.observations import format_observations

    bus = Bus(settings)
    await bus.connect()
    try:
        observations = await bus.list_observations()
        print(format_observations(observations, args.model, args.limit))
        return 0
    finally:
        await bus.close()


async def cmd_pheromone(args: argparse.Namespace, settings: Settings) -> int:
    """14.15.10: pheromone field view (ACO strategy scores per task type)."""
    from memnc.pheromone import format_pheromones

    bus = Bus(settings)
    await bus.connect()
    try:
        entries = await bus.pheromone_entries()
        if getattr(args, "pick", None):
            from memnc.pheromone import choose_edge

            edge = choose_edge(entries, args.task_type)
            print(f"pick {args.task_type} -> {edge}")
        else:
            print(format_pheromones(entries, args.task_type))
        return 0
    finally:
        await bus.close()


async def cmd_similar(args: argparse.Namespace, settings: Settings) -> int:
    """14.13 step 4: ranked attention over deposited observations (read-only)."""
    from memnc.experience import format_ranked, ranked_attention

    bus = Bus(settings)
    await bus.connect()
    try:
        observations = await bus.list_observations()
        if args.task_id:
            source = next(
                (o for o in observations if o.task_id == args.task_id), None
            )
            if source is None:
                print(f"unknown observation task id: {args.task_id}")
                return 1
            observations = [o for o in observations if o.task_id != args.task_id]
            query = source.task
        else:
            query = " ".join(args.query).strip()
            if not query:
                print("provide a query text or --task-id")
                return 1
        results = ranked_attention(
            query,
            observations,
            half_life_days=args.half_life,
        )
        print(format_ranked(results, args.limit))
        return 0
    finally:
        await bus.close()


async def cmd_synergy(_: argparse.Namespace, settings: Settings) -> int:
    """14.13 step 3: aggregated model-pair synergy (measurement only)."""
    from memnc.synergy import compute_synergy_metrics, format_synergy

    bus = Bus(settings)
    await bus.connect()
    try:
        observations = await bus.list_observations()
        print(format_synergy(compute_synergy_metrics(observations), _.limit))
        return 0
    finally:
        await bus.close()


async def cmd_backup(args: argparse.Namespace, settings: Settings) -> int:
    """Cold-standby backup: dump durable KV buckets to a JSON file."""
    bus = Bus(settings)
    await bus.connect()
    try:
        data = await dump_buckets(bus)
    finally:
        await bus.close()
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    counts = {k: len(v) for k, v in data["buckets"].items()}
    print(f"backup written to {args.out}: {counts}")
    return 0


async def cmd_restore(args: argparse.Namespace, settings: Settings) -> int:
    """Cold-standby restore: write a backup JSON back into the KV buckets."""
    with open(args.file, encoding="utf-8") as fh:
        data = json.load(fh)
    bus = Bus(settings)
    await bus.connect()
    try:
        counts = await restore_buckets(bus, data, purge=args.purge)
    finally:
        await bus.close()
    print(f"restored: {counts}")
    return 0


async def cmd_roster(_: argparse.Namespace, settings: Settings) -> int:
    """T6: list live node roster entries (capabilities + presence)."""
    bus = Bus(settings)
    await bus.connect()
    try:
        nodes = await bus.list_nodes()
        if not nodes:
            print("no nodes in roster")
            return 0
        for node in nodes:
            print(
                f"{node.node_id}  roles={','.join(node.roles) or '-'}  "
                f"models={','.join(node.models) or '-'}  "
                f"disk={','.join(node.disk_models) or '-'}  "
                f"vram={node.vram_gb or 0:g}GB  gpu={node.gpu or '-'}  "
                f"updated={node.updated_at}"
            )
        return 0
    finally:
        await bus.close()


async def cmd_mreqs(_: argparse.Namespace, settings: Settings) -> int:
    """T7.5: list model requests (pending first)."""
    bus = Bus(settings)
    await bus.connect()
    try:
        requests = await bus.list_model_requests()
        if not requests:
            print("no model requests")
            return 0
        for r in requests:
            disk = f" disk@{','.join(r.nodes_disk)}" if r.nodes_disk else ""
            print(
                f"{r.request_id}  {r.status}  {r.action}  {r.model}{disk}"
                f"  task={r.task_id or '-'}  decided={r.decided_by or '-'}"
            )
        return 0
    finally:
        await bus.close()


async def cmd_decide(args: argparse.Namespace, settings: Settings) -> int:
    """T7.5: approve or reject a model request (human gate)."""
    bus = Bus(settings)
    await bus.connect()
    try:
        status = "approved" if args.action == "approve" else "rejected"
        matches = [
            r for r in await bus.list_model_requests() if r.request_id.startswith(args.request_id)
        ]
        if not matches:
            print(f"no model request matching {args.request_id}")
            return 1
        request = matches[0]
        if request.status != "pending":
            print(f"{request.request_id} already {request.status}")
            return 1
        # T7: an approved pull is refused when the model is not whitelisted.
        if (
            status == "approved"
            and request.action == "pull"
            and not model_pull_allowed(request.model, settings.model_pull_whitelist)
        ):
            print(f"refused: {request.model} not in model_pull_whitelist")
            return 1
        request.status = status
        request.decided_by = args.by
        request.decided_at = datetime.now(UTC).isoformat()
        await bus.put_model_request(request)
        await bus.publish_model_decision(request)
        print(f"{request.request_id} {status} ({request.model})")
        return 0
    finally:
        await bus.close()


async def cmd_gate(args: argparse.Namespace, settings: Settings) -> int:
    """T3: approve or reject a task parked at the approval gate."""
    client = await _connect(settings)
    cmd = ControlCommand(
        action="decide",
        agent_id=args.by,
        task_id=args.task_id.lstrip("#"),
        approved=args.action == "approve",
    )
    try:
        response = await client.request(SUBJECT_CONTROL, cmd.model_dump_json().encode(), timeout=15)
        data = json.loads(response.data)
        if "error" in data:
            print(f"error: {data['error']}", file=sys.stderr)
            return 1
        print(f"{data['task_id']} {data['verdict']} -> {data['state']}")
        return 0
    finally:
        await client.close()


async def cmd_answer(args: argparse.Namespace, settings: Settings) -> int:
    """Reply to a task parked in WAIT_INPUT (an agent asked a question)."""
    client = await _connect(settings)
    cmd = ControlCommand(
        action="answer",
        agent_id=args.by,
        task_id=args.task_id.lstrip("#"),
        text=" ".join(args.text),
    )
    try:
        response = await client.request(SUBJECT_CONTROL, cmd.model_dump_json().encode(), timeout=15)
        data = json.loads(response.data)
        if "error" in data:
            print(f"error: {data['error']}", file=sys.stderr)
            return 1
        print(f"{data['task_id']} answered -> {data['state']}")
        return 0
    finally:
        await client.close()


async def cmd_pause(args: argparse.Namespace, settings: Settings) -> int:
    """T4: pause or resume a node's claim loop."""
    client = await _connect(settings)
    cmd = ControlCommand(action=args.verb, agent_id=args.by, node=args.node)
    try:
        response = await client.request(SUBJECT_CONTROL, cmd.model_dump_json().encode(), timeout=15)
        data = json.loads(response.data)
        if "error" in data:
            print(f"error: {data['error']}", file=sys.stderr)
            return 1
        state = "paused" if data["paused"] else "resumed"
        print(f"{data['node']} {state}")
        return 0
    finally:
        await client.close()


async def cmd_registry(_: argparse.Namespace, settings: Settings) -> int:
    bus = Bus(settings)
    await bus.connect()
    try:
        capabilities = await bus.list_capabilities()
        if not capabilities:
            print("no capabilities advertised")
            return 0
        for cap in sorted(capabilities, key=lambda c: (c.agent_id, c.skill)):
            print(
                f"{cap.agent_id}  {cap.skill:<20} node={cap.node or '-'}  "
                f"load={cap.load:.2f}/{cap.capacity}  model={cap.model or '-'}  "
                f"contact={cap.contact or '-'}"
            )
        print(f"{len(capabilities)} capabilities advertised")
        return 0
    finally:
        await bus.close()


async def cmd_ledger(args: argparse.Namespace, settings: Settings) -> int:
    """M10: read the queryable mesh ledger.

    Preferred path is the agent-facing request/reply on mesh.ledger (any
    granted agent can do this); if no orchestrator answers, fall back to
    direct KV reads via the Bus.
    """
    client = await _connect(settings)
    try:
        try:
            response = await client.request(SUBJECT_LEDGER, b"{}", timeout=5)
            data = json.loads(response.data)
        except Exception:
            bus = Bus(settings)
            await bus.connect()
            try:
                data = await bus.read_ledger()
            finally:
                await bus.close()
        print(json.dumps(data, indent=2))
        return 0
    finally:
        await client.close()


async def cmd_grant(args: argparse.Namespace, settings: Settings) -> int:
    bus = Bus(settings)
    await bus.connect()
    try:
        level = GrantLevel(args.level)
        grant = await bus.issue_grant(
            args.agent_id, level=level, granted_by=args.granted_by, ttl_s=args.ttl_s
        )
        print(
            f"granted {grant.level.value} to {grant.agent_id} (by {grant.granted_by},"
            f" ttl={args.ttl_s or 'never'} s)"
        )
        return 0
    finally:
        await bus.close()


async def cmd_grants(_: argparse.Namespace, settings: Settings) -> int:
    bus = Bus(settings)
    await bus.connect()
    try:
        grants = await bus.list_grants()
        if not grants:
            print("no grants issued")
            return 0
        now = time.time()
        for grant in sorted(grants, key=lambda g: g.agent_id):
            expiry = (
                f"expires in {grant.expires_at - now:.0f}s"
                if grant.expires_at > 0
                else "never expires"
            )
            print(f"{grant.agent_id}  {grant.level.value:<5}  by={grant.granted_by}  {expiry}")
        print(f"{len(grants)} grants on file")
        return 0
    finally:
        await bus.close()


async def cmd_revoke_grant(args: argparse.Namespace, settings: Settings) -> int:
    bus = Bus(settings)
    await bus.connect()
    try:
        await bus.revoke_grant(args.agent_id)
        print(f"revoked grant for {args.agent_id}")
        return 0
    finally:
        await bus.close()


async def cmd_revoke_node(args: argparse.Namespace, settings: Settings) -> int:
    """M7: revoke a whole node — roster cut + capability withdrawal + claim gate."""
    bus = Bus(settings)
    await bus.connect()
    try:
        record = await bus.revoke_node(args.node_id, reason=args.reason, by=args.by)
        print(f"revoked node {record.node_id} (reason={record.reason or '-'} by={record.by})")
        return 0
    finally:
        await bus.close()


async def cmd_reinstate_node(args: argparse.Namespace, settings: Settings) -> int:
    bus = Bus(settings)
    await bus.connect()
    try:
        if await bus.reinstate_node(args.node_id):
            print(f"reinstate: tombstone for {args.node_id} removed")
        else:
            print(f"{args.node_id} was not revoked")
        return 0
    finally:
        await bus.close()


async def cmd_revocations(_: argparse.Namespace, settings: Settings) -> int:
    bus = Bus(settings)
    await bus.connect()
    try:
        revocations = await bus.list_revocations()
        if not revocations:
            print("no revoked nodes")
            return 0
        for record in sorted(revocations, key=lambda r: r.node_id):
            print(f"{record.node_id}  by={record.by}  reason={record.reason or '-'}")
        print(f"{len(revocations)} node(s) revoked")
        return 0
    finally:
        await bus.close()


async def cmd_doctor(_: argparse.Namespace, settings: Settings) -> int:
    ok = True
    print(f"NATS       {settings.nats_url}")
    bus = Bus(settings)
    try:
        # Wrap so an unresolvable NATS host (e.g. the Docker service name `nats`
        # from .env used from the host shell) degrades to FAILED instead of
        # hanging in the reconnect/DNS loop. Plan §9/§12: warn, don't block.
        await asyncio.wait_for(bus.connect(attempts=3), timeout=8)
        assert bus.js is not None
        info = await bus.js.stream_info(settings.stream_name)
        print(f"stream     {info.config.name}  messages={info.state.messages}")
        print(f"kv         {settings.kv_bucket}  ok")
        # P4 slice 2: report nodes whose roster fingerprint differs from
        # this host's code — the upgrade list for the fleet.
        try:
            nodes = await bus.list_nodes()
            mine = code_version()
            print(f"code       {mine}")
            for node in nodes:
                stale = is_stale(node.version, mine)
                mark = "STALE" if stale else "ok"
                print(f"  node {node.node_id:<20} {node.version}  {mark}")
                if stale:
                    ok = False
        except Exception:  # noqa: BLE001 - roster view is best effort
            print("roster     unavailable", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"stream     FAILED: {exc}", file=sys.stderr)
        ok = False

    backend = make_backend(settings)
    try:
        models = await backend.list_models()
        print(
            f"inference  {settings.inference_provider} @ "
            f"{settings.inference_base_url}  models={len(models)}"
        )
        for role, model in settings.models().items():
            present = _model_present(model, models)
            mark = "ok" if present else "MISSING"
            print(f"  {role.value:<8} {model}  {mark}")
            if not present:
                ok = False
    except Exception as exc:  # noqa: BLE001
        print(f"inference  FAILED: {exc}", file=sys.stderr)
        ok = False
    finally:
        await backend.close()
        await bus.close()

    print("healthy" if ok else "degraded")
    return 0 if ok else 1


def _model_present(model: str, models: list[str]) -> bool:
    if model in models or f"{model}:latest" in models:
        return True
    base = model.split(":")[0]
    return any(candidate.split(":")[0] == base for candidate in models)


async def _backlog_store(client) -> BacklogStore:
    return BacklogStore(client.jetstream())


async def _backlog_submit(
    client, settings: Settings, prompt: str, prefer_node: str = "", harness: str = ""
) -> str:
    """Submit one backlog task to the mesh, return the mesh task id."""
    submission = TaskSubmission(
        task=prompt, context="", fanout=1, claimable=True,
        prefer_node=prefer_node, harness=harness,
    )
    response = await client.request(
        SUBJECT_SUBMIT_NODE.format(node=settings.node_name),
        submission.model_dump_json().encode(),
        timeout=10,
    )
    data = json.loads(response.data)
    if "error" in data:
        raise RuntimeError(data["error"])
    return str(data["task_id"])


async def _backlog_watch(client, task_id: str) -> str:
    """Follow a mesh task to a terminal event; return the final event name.

    JetStream ordered consumer (deliver_all): fast tasks can hit a terminal
    event before the core-NATS subscription would be live, so the watcher
    must receive the retained history too.
    """
    subject = SUBJECT_EVENTS.format(task_id=task_id)
    subscription = await client.jetstream().subscribe(subject, ordered_consumer=True)
    try:
        while True:
            try:
                message = await subscription.next_msg(timeout=5.0)
            except Exception:
                continue
            try:
                envelope = json.loads(message.data)
            except json.JSONDecodeError:
                continue
            event = (envelope.get("payload") or {}).get("event", "")
            if event in _TERMINAL_EVENTS:
                return event
    finally:
        await subscription.unsubscribe()


async def cmd_backlog_add(args: argparse.Namespace, settings: Settings) -> int:
    client = await _connect(settings)
    try:
        store = await _backlog_store(client)
        task = await store.add(
            title=args.title,
            prompt=args.prompt or args.title,
            plan_id=args.plan_id or "",
            deps=args.dep or [],
            harness=args.harness or "",
        )
        print(task.id)
        return 0
    finally:
        await client.close()


async def cmd_backlog_list(args: argparse.Namespace, settings: Settings) -> int:
    client = await _connect(settings)
    try:
        store = await _backlog_store(client)
        tasks = await store.list()
        if args.plan_id:
            tasks = [t for t in tasks if t.plan_id == args.plan_id]
        for t in tasks:
            print(
                f"{t.id}  {t.status:<8}  plan={t.plan_id or '-':<12} "
                f"attempts={t.attempts}/{MAX_ATTEMPTS}  "
                f"mesh={t.mesh_task_id or '-':<20}  {t.title}"
            )
        if not tasks:
            print("backlog empty")
        return 0
    finally:
        await client.close()


async def _driver_cycle(client, settings: Settings, driver: str) -> bool:
    """One claim->submit->watch->mark cycle. Returns True when work was done."""
    store = await _backlog_store(client)
    claimed = await store.next_claimable(driver)
    if claimed is None:
        return False
    print(f"{driver}: claimed {claimed.id} ({claimed.title})", flush=True)
    try:
        mesh_task_id = await _backlog_submit(
            client,
            settings,
            claimed.prompt,
            prefer_node=claimed.prefer_node,
            harness=claimed.harness,
        )
        await store.mark(claimed.id, "claimed", mesh_task_id=mesh_task_id)
        event = await _backlog_watch(client, mesh_task_id)
        final = "done" if event in ("task.done", "state.done") else "failed"
        await store.mark(claimed.id, final, mesh_task_id=mesh_task_id)
        print(f"{driver}: {claimed.id} -> {final} (mesh {mesh_task_id})", flush=True)
    except Exception as exc:  # noqa: BLE001 - failed submission burns one attempt
        final = "failed"
        await store.mark(claimed.id, "failed")
        print(f"{driver}: {claimed.id} -> failed ({exc})", flush=True)
    # Weak->strong routing: one-shot strong-node retry after the attempts
    # budget is exhausted (mark() flips back to planned while budget lasts).
    if final == "failed":
        task = await store.get(claimed.id)
        if task and should_strong_retry(task, settings.strong_node):
            retry = make_strong_retry(task, settings.strong_node)
            await store.add(
                title=retry.title,
                prompt=retry.prompt,
                plan_id=retry.plan_id,
                deps=retry.deps,
                prefer_node=retry.prefer_node,
                strong_retry=True,
                harness=retry.harness,
            )
            print(
                f"{driver}: strong-retry queued (prefer {retry.prefer_node}) for {claimed.id}",
                flush=True,
            )
    return True


async def _backlog_plan(args: argparse.Namespace, settings: Settings) -> int:
    """Decompose a plan text into backlog entries sharing a plan_id."""
    tasks = decompose_plan(args.plan)
    if not tasks:
        print("error: plan text is empty", file=sys.stderr)
        return 1
    if args.dry_run:
        for i, text in enumerate(tasks, 1):
            print(f"{i}. {text}")
        return 0

    client = await _connect(settings)
    try:
        store = await _backlog_store(client)
        prev_id = ""
        for i, text in enumerate(tasks, 1):
            words = text.split()
            title = " ".join(words[:8]) + ("..." if len(words) > 8 else "")
            deps = [prev_id] if args.chain and prev_id else []
            task = await store.add(
                title=title, prompt=text, plan_id=args.plan_id, deps=deps,
                harness=args.harness or "",
            )
            prev_id = task.id
            print(f"added {task.id} [{i}/{len(tasks)}] deps={deps} {title}")
        return 0
    finally:
        await client.close()


def cmd_backlog_plan(args: argparse.Namespace, settings: Settings):
    return _backlog_plan(args, settings)


async def cmd_backlog_reset(args: argparse.Namespace, settings: Settings) -> int:
    client = await _connect(settings)
    try:
        store = await _backlog_store(client)
        task = await store.get(args.backlog_id)
        if task is None:
            print("not found")
            return 1
        task.status = "planned"
        task.claimant = ""
        task.claimed_at = ""
        task.mesh_task_id = ""
        task.updated_at = task.updated_at
        kv = await store._ensure_kv()
        await kv.put(task.id, task.model_dump_json().encode())
        print(f"{task.id} -> planned")
        return 0
    finally:
        await client.close()


async def _maybe_wake_idle(
    get_store, settings: Settings, waker: WolWaker
) -> bool:
    """P4 slice 3: planned backlog work + WOL config -> wake the reserve host.

    ``get_store`` is a zero-arg callable returning the BacklogStore (in the
    driver loop: a partial of ``_backlog_store`` over the connected client).
    Throttling lives in the WolWaker, so an idle loop cannot flood the
    sleeping host with packets.
    """
    if not waker.enabled:
        return False
    result = get_store() if callable(get_store) else get_store
    store = await result if inspect.isawaitable(result) else result
    planned = [t for t in await store.list() if t.status == "planned"]
    if not planned:
        return False
    sent = waker.maybe_wake()
    if sent:
        print(f"backlog: {len(planned)} planned -> WOL packet sent", flush=True)
    return sent


async def cmd_backlog_run(args: argparse.Namespace, settings: Settings) -> int:
    client = await _connect(settings)
    waker = WolWaker(settings.wol_mac, settings.wol_min_interval_s)
    try:
        driver = args.driver or f"driver-{settings.node_name}"
        if args.once:
            return 0 if await _driver_cycle(client, settings, driver) else 0
        while True:
            try:
                worked = await _driver_cycle(client, settings, driver)
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                print(f"driver error: {exc}", file=sys.stderr, flush=True)
                worked = False
            if not worked:
                await _maybe_wake_idle(lambda: _backlog_store(client), settings, waker)
                await asyncio.sleep(args.idle_sleep)
    finally:
        await client.close()


def cmd_wol(args: argparse.Namespace, settings: Settings) -> int:
    mac = args.mac or settings.wol_mac
    if not mac:
        print("no MAC: pass --mac or set WOL_MAC", file=sys.stderr)
        return 2
    try:
        send_wol(mac)
    except ValueError as exc:
        print(f"invalid MAC: {exc}", file=sys.stderr)
        return 2
    print(f"WOL packet sent to {mac}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="meshctl")
    subparsers = parser.add_subparsers(dest="command", required=True)

    submit = subparsers.add_parser("submit", help="submit a task")
    submit.add_argument("task")
    submit.add_argument("--context", default="")
    submit.add_argument(
        "--watch", action="store_true", help="follow the task's events until it finishes"
    )
    submit.add_argument(
        "--fanout", action="store_true", help="M10: split the plan into independent subtasks"
    )
    submit.add_argument(
        "--claimable", action="store_true", help="M11: leave in RECEIVED for agents to self-claim"
    )
    submit.add_argument(
        "--node", default=None, help="M15: target orchestrator node (default: local node)"
    )
    submit.add_argument(
        "--needs-vram", type=float, default=0.0, help="T6: VRAM (GB) the claiming node must have"
    )
    submit.add_argument(
        "--prefer",
        default="",
        help="Weak->strong routing: node_name prefix the task prefers",
    )
    submit.add_argument(
        "--needs-model", default="", help="T6: model name the claiming node must serve"
    )
    submit.add_argument(
        "--needs-role", action="append", default=None, help="T6: allowed claiming role (repeatable)"
    )
    submit.add_argument(
        "--gate",
        default="never",
        choices=["always", "fail", "never"],
        help="T3: human approval gate at the critique step",
    )
    submit.add_argument(
        "--harness", default="", help="14.14: coder harness for this task (e.g. pi); empty = node default"
    )
    submit.set_defaults(func=cmd_submit)

    gate = subparsers.add_parser(
        "gate", help="T3: approve/reject a task parked at the approval gate"
    )
    gate.add_argument("action", choices=["approve", "reject"])
    gate.add_argument("task_id", help="parked task id (prefix not supported)")
    gate.add_argument("--by", default="meshctl")
    gate.set_defaults(func=cmd_gate)

    answer = subparsers.add_parser(
        "answer", help="reply to a task parked in WAIT_INPUT (agent asked)"
    )
    answer.add_argument("task_id", help="task id (prefix match)")
    answer.add_argument("text", nargs="+", help="the human's reply")
    answer.add_argument("--by", default="meshctl")
    answer.set_defaults(func=cmd_answer)

    pause = subparsers.add_parser("pause", help="T4: stop a node from claiming work")
    pause.add_argument("node")
    pause.add_argument("--by", default="meshctl")
    pause.set_defaults(func=cmd_pause, verb="pause_node")

    resume = subparsers.add_parser("resume", help="T4: let a paused node claim again")
    resume.add_argument("node")
    resume.add_argument("--by", default="meshctl")
    resume.set_defaults(func=cmd_pause, verb="resume_node")

    roster = subparsers.add_parser("roster", help="T6: list live node capabilities")
    roster.set_defaults(func=cmd_roster)

    exp = subparsers.add_parser(
        "exp", help="15.3 item 5: read the automated experience store"
    )
    exp.add_argument("--topic", default=None, help="only bullets in TOPIC")
    exp.add_argument(
        "--limit", type=int, default=50, help="max bullets to print (default 50)"
    )
    exp.set_defaults(func=cmd_exp)

    obslast = subparsers.add_parser(
        "obslast", help="14.13 step 2: latest deposited task observations"
    )
    obslast.add_argument("--model", default=None, help="only tasks served by MODEL")
    obslast.add_argument("--limit", type=int, default=10)
    obslast.set_defaults(func=cmd_obslast)

    pheromone = subparsers.add_parser(
        "pheromone", help="14.15.10: pheromone routing field (ACO scores)"
    )
    pheromone.add_argument("--task-type", default=None, help="filter by task type slug")
    pheromone.add_argument(
        "--pick",
        metavar="TASK_TYPE",
        default=None,
        help="choose the best edge for TASK_TYPE from the live field",
    )
    pheromone.set_defaults(func=cmd_pheromone)

    synergy = subparsers.add_parser(
        "synergy", help="14.13 step 3: aggregated model-pair synergy"
    )
    synergy.add_argument("--limit", type=int, default=10)
    synergy.set_defaults(func=cmd_synergy)

    similar = subparsers.add_parser(
        "similar",
        help="14.13 step 4: rank past task observations against a query",
    )
    similar.add_argument("query", nargs="*", help="free-text task query")
    similar.add_argument(
        "--task-id", default=None, help="use this deposited observation's task as query"
    )
    similar.add_argument("--limit", type=int, default=10)
    similar.add_argument("--half-life", type=float, default=14.0, help="days")
    similar.set_defaults(func=cmd_similar)

    tail = subparsers.add_parser("tail", help="follow a task's event stream")
    tail.add_argument("task_id")
    tail.add_argument("--limit", type=int, default=None)
    tail.set_defaults(func=cmd_tail)

    state = subparsers.add_parser("state", help="print a task snapshot")
    state.add_argument("task_id")
    state.set_defaults(func=cmd_state)
    trace = subparsers.add_parser("trace", help="T5: replay a task's coder/critic debate")
    trace.add_argument("task_id")
    trace.set_defaults(func=cmd_trace)

    tasks = subparsers.add_parser("tasks", help="list tasks")
    tasks.set_defaults(func=cmd_tasks)

    mreqs = subparsers.add_parser("mreqs", help="T7.5: list model requests (pending first)")
    mreqs.set_defaults(func=cmd_mreqs)

    decide = subparsers.add_parser(
        "decide", help="T7.5: approve/reject a model request (human gate)"
    )
    decide.add_argument("action", choices=["approve", "reject"])
    decide.add_argument("request_id", help="request id (prefix match)")
    decide.add_argument("--by", default="meshctl")
    decide.set_defaults(func=cmd_decide)

    registry = subparsers.add_parser("registry", help="list advertised capability records")
    registry.set_defaults(func=cmd_registry)

    ledger = subparsers.add_parser(
        "ledger", help="M10: query the mesh ledger (registry+grants+trust+tasks)"
    )
    ledger.set_defaults(func=cmd_ledger)

    grant = subparsers.add_parser("grant", help="issue a capability grant (M9 permission chain)")
    grant.add_argument("agent_id")
    grant.add_argument("--level", default="chain", choices=[lv.value for lv in GrantLevel])
    grant.add_argument("--granted-by", default="orchestrator")
    grant.add_argument("--ttl-s", type=float, default=0, help="expiry in seconds (0 = never)")
    grant.set_defaults(func=cmd_grant)

    grants = subparsers.add_parser("grants", help="list issued capability grants")
    grants.set_defaults(func=cmd_grants)

    revoke = subparsers.add_parser("revoke", help="revoke an agent's capability grant")
    revoke.add_argument("agent_id")
    revoke.set_defaults(func=cmd_revoke_grant)
    revoke_node = subparsers.add_parser(
        "revoke-node", help="M7: revoke a whole node (roster cut, caps withdrawn, claims denied)"
    )
    revoke_node.add_argument("node_id")
    revoke_node.add_argument("--reason", default="")
    revoke_node.add_argument("--by", default="meshctl")
    revoke_node.set_defaults(func=cmd_revoke_node)
    reinstate = subparsers.add_parser(
        "reinstate", help="M7: remove a node revocation tombstone (node rejoins on next heartbeat)"
    )
    reinstate.add_argument("node_id")
    reinstate.set_defaults(func=cmd_reinstate_node)
    revocations = subparsers.add_parser("revocations", help="list revoked nodes")
    revocations.set_defaults(func=cmd_revocations)

    backup = subparsers.add_parser(
        "backup", help="cold-standby backup: dump durable KV buckets to JSON"
    )
    backup.add_argument("--out", required=True, help="output JSON file")
    backup.set_defaults(func=cmd_backup)

    restore = subparsers.add_parser(
        "restore", help="cold-standby restore: write a backup JSON back into KV"
    )
    restore.add_argument("file", help="backup JSON file")
    restore.add_argument(
        "--purge",
        action="store_true",
        help="delete live keys missing from the backup (exact mirror)",
    )
    restore.set_defaults(func=cmd_restore)

    doctor = subparsers.add_parser("doctor", help="check connectivity and models")
    doctor.set_defaults(func=cmd_doctor)

    backlog_add = subparsers.add_parser(
        "backlog-add", help="add a task to the backlog (optionally under a plan)"
    )
    backlog_add.add_argument("title")
    backlog_add.add_argument("--prompt", default="", help="coder prompt; defaults to title")
    backlog_add.add_argument("--plan-id", default="")
    backlog_add.add_argument("--dep", action="append", help="backlog id this depends on")
    backlog_add.add_argument(
        "--harness", default="",
        help="coder harness for this task (e.g. pi); empty = node default",
    )
    backlog_add.set_defaults(func=cmd_backlog_add)

    backlog_list = subparsers.add_parser("backlog-list", help="list backlog entries")
    backlog_list.add_argument("--plan-id", default="", help="filter by plan")
    backlog_list.set_defaults(func=cmd_backlog_list)

    plan_parser = subparsers.add_parser(
        "backlog-plan",
        help="decompose a plan text into backlog tasks sharing a plan_id",
    )
    plan_parser.add_argument("plan", help="plan text (numbered/bullet lines or paragraphs)")
    plan_parser.add_argument("--plan-id", required=True)
    plan_parser.add_argument("--chain", action="store_true",
                             help="make each task depend on the previous one (sequential plan)")
    plan_parser.add_argument("--dry-run", action="store_true", help="print the tasks without adding")
    plan_parser.add_argument(
        "--harness", default="",
        help="coder harness for every generated task (e.g. pi); empty = node default",
    )
    plan_parser.set_defaults(func=cmd_backlog_plan)

    backlog_reset = subparsers.add_parser("backlog-reset", help="re-plan a backlog entry")
    backlog_reset.add_argument("backlog_id")
    backlog_reset.set_defaults(func=cmd_backlog_reset)

    wol = subparsers.add_parser(
        "wol", help="send one Wake-on-LAN magic packet (P4: wake-on-backlog)"
    )
    wol.add_argument("--mac", default="", help="target MAC (default: WOL_MAC setting)")
    wol.set_defaults(func=cmd_wol)

    backlog_run = subparsers.add_parser(
        "backlog-run", help="driver loop: claim -> submit -> watch -> mark"
    )
    backlog_run.add_argument("--once", action="store_true", help="single cycle then exit")
    backlog_run.add_argument("--driver", default="", help="driver/claimant name")
    backlog_run.add_argument("--node", default="", help="orchestrator node to submit to")
    backlog_run.add_argument("--idle-sleep", type=float, default=10.0)
    backlog_run.set_defaults(func=cmd_backlog_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = get_settings()
    try:
        return asyncio.run(args.func(args, settings))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
