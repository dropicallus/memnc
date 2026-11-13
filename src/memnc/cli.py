"""meshctl: submit tasks, tail events, inspect state, check connectivity."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import UTC, datetime

import nats

from .backends import make_backend
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


_TERMINAL_EVENTS = {"task.done", "task.failed", "task.cancelled"}


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


async def cmd_revoke(args: argparse.Namespace, settings: Settings) -> int:
    bus = Bus(settings)
    await bus.connect()
    try:
        await bus.revoke_grant(args.agent_id)
        print(f"revoked grant for {args.agent_id}")
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

    obslast = subparsers.add_parser(
        "obslast", help="14.13 step 2: latest deposited task observations"
    )
    obslast.add_argument("--model", default=None, help="only tasks served by MODEL")
    obslast.add_argument("--limit", type=int, default=10)
    obslast.set_defaults(func=cmd_obslast)

    synergy = subparsers.add_parser(
        "synergy", help="14.13 step 3: aggregated model-pair synergy"
    )
    synergy.add_argument("--limit", type=int, default=10)
    synergy.set_defaults(func=cmd_synergy)

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
    revoke.set_defaults(func=cmd_revoke)

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
