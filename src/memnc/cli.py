"""meshctl: submit tasks, tail events, inspect state, check connectivity."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

import nats

from .backends import make_backend
from .bus import Bus
from .config import Settings, get_settings
from .schema import (
    SUBJECT_EVENTS,
    SUBJECT_LEDGER,
    SUBJECT_SUBMIT,
    SUBJECT_SUBMIT_NODE,
    TERMINAL_STATES,
    GrantLevel,
    TaskNeeds,
    TaskSubmission,
)


async def _connect(settings: Settings):
    return await nats.connect(
        settings.nats_url,
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
        )
        response = await client.request(
            # M15: owner-routed submission — default to this machine's node
            # name so any node's meshctl submits to its own orchestrator.
            SUBJECT_SUBMIT_NODE.format(
                node=getattr(args, "node", None) or settings.node_name
            ),
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
            print(f"{envelope.get('ts','')} {event:<24} {detail}".rstrip())
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
            print(
                f"{envelope.get('ts','')} {state:<9} {event:<24} {detail}".rstrip()
            )
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
        print(f"granted {grant.level.value} to {grant.agent_id} (by {grant.granted_by},"
              f" ttl={args.ttl_s or 'never'} s)")
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
    submit.add_argument("--watch", action="store_true",
                        help="follow the task's events until it finishes")
    submit.add_argument("--fanout", action="store_true",
                        help="M10: split the plan into independent subtasks")
    submit.add_argument("--claimable", action="store_true",
                        help="M11: leave in RECEIVED for agents to self-claim")
    submit.add_argument("--node", default=None,
                        help="M15: target orchestrator node (default: local node)")
    submit.add_argument("--needs-vram", type=float, default=0.0,
                        help="T6: VRAM (GB) the claiming node must have")
    submit.add_argument("--needs-model", default="",
                        help="T6: model name the claiming node must serve")
    submit.add_argument("--needs-role", action="append", default=None,
                        help="T6: allowed claiming role (repeatable)")
    submit.set_defaults(func=cmd_submit)

    roster = subparsers.add_parser("roster", help="T6: list live node capabilities")
    roster.set_defaults(func=cmd_roster)

    tail = subparsers.add_parser("tail", help="follow a task's event stream")
    tail.add_argument("task_id")
    tail.add_argument("--limit", type=int, default=None)
    tail.set_defaults(func=cmd_tail)

    state = subparsers.add_parser("state", help="print a task snapshot")
    state.add_argument("task_id")
    state.set_defaults(func=cmd_state)

    tasks = subparsers.add_parser("tasks", help="list tasks")
    tasks.set_defaults(func=cmd_tasks)

    registry = subparsers.add_parser("registry", help="list advertised capability records")
    registry.set_defaults(func=cmd_registry)

    ledger = subparsers.add_parser("ledger", help="M10: query the mesh ledger (registry+grants+trust+tasks)")
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
