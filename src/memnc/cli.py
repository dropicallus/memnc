"""meshctl: submit tasks, tail events, inspect state, check connectivity."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import nats

from .backends import make_backend
from .bus import Bus
from .config import Settings, get_settings
from .schema import SUBJECT_EVENTS, SUBJECT_SUBMIT, TERMINAL_STATES, TaskSubmission


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
        submission = TaskSubmission(task=args.task, context=args.context or "")
        response = await client.request(
            SUBJECT_SUBMIT, submission.model_dump_json().encode(), timeout=10
        )
        data = json.loads(response.data)
        if "error" in data:
            print(f"error: {data['error']}", file=sys.stderr)
            return 1
        print(data["task_id"])
        return 0
    finally:
        await client.close()


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
    submit.set_defaults(func=cmd_submit)

    tail = subparsers.add_parser("tail", help="follow a task's event stream")
    tail.add_argument("task_id")
    tail.add_argument("--limit", type=int, default=None)
    tail.set_defaults(func=cmd_tail)

    state = subparsers.add_parser("state", help="print a task snapshot")
    state.add_argument("task_id")
    state.set_defaults(func=cmd_state)

    tasks = subparsers.add_parser("tasks", help="list tasks")
    tasks.set_defaults(func=cmd_tasks)

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
