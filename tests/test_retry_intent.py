"""Retry-with-intent (P1 item 6): a human retry can force a different model.

Chain: Telegram/api write verb -> ControlCommand.model_override ->
orchestrator stamps TaskSnapshot.model_override and re-dispatches ->
dispatch payload carries model_override -> worker uses it instead of the
role default for every re-dispatch of that snapshot.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from test_control import _GRANTED, FakeBus, _cmd, _FakeMsg, _orch
from test_telegram_commands import _FakeBot, _observer

import memnc.worker as worker_mod
from memnc.config import Settings
from memnc.orchestrator import Orchestrator
from memnc.schema import (
    ControlCommand,
    Role,
    TaskSnapshot,
    TaskState,
)
from memnc.worker import Worker


def _failed_task(orch: Orchestrator) -> str:
    resp = _run(orch, _cmd())
    task_id = resp["task_id"]
    orch.bus.snapshots[task_id].state = TaskState.FAILED
    return task_id


def _run(orch: Orchestrator, cmd: ControlCommand) -> dict:
    msg = _FakeMsg(cmd.model_dump_json().encode())
    asyncio.run(orch._on_control(msg))
    assert msg.replied is not None
    return json.loads(msg.replied)


def test_retry_with_model_override_stamps_snapshot():
    orch = _orch({"telegram-123": _GRANTED})
    task_id = _failed_task(orch)
    resp = _run(
        orch,
        _cmd(action="retry", task_id=task_id, task="", model_override="qwen2.5:7b"),
    )
    assert resp["ok"] is True
    snapshot = orch.bus.snapshots[task_id]
    assert snapshot.model_override == "qwen2.5:7b"
    assert snapshot.state is TaskState.PLANNING


def test_plain_retry_keeps_existing_override():
    orch = _orch({"telegram-123": _GRANTED})
    task_id = _failed_task(orch)
    _run(orch, _cmd(action="retry", task_id=task_id, task="", model_override="big:32b"))
    orch.bus.snapshots[task_id].state = TaskState.FAILED
    _run(orch, _cmd(action="retry", task_id=task_id, task=""))
    assert orch.bus.snapshots[task_id].model_override == "big:32b"


def test_dispatch_payload_carries_override():
    orch = _orch({"telegram-123": _GRANTED})
    task_id = _failed_task(orch)
    _run(orch, _cmd(action="retry", task_id=task_id, task="", model_override="big:32b"))
    envelopes = [env for _subj, env in orch.bus.published if env.role == Role.PLANNER]
    assert envelopes, "retry must re-dispatch a planner envelope"
    assert any(env.payload.get("model_override") == "big:32b" for env in envelopes)


def test_worker_uses_model_override_in_run_impl():
    settings = Settings(role="coder", node_name="node-a", model_coder="default-m")
    worker = Worker(settings, bus=FakeBus(), backend=object())  # type: ignore[arg-type]
    snapshot = TaskSnapshot(
        task_id="ovr-1", trace_id="t1", task="do it", state=TaskState.PLANNING
    )
    envelope = SimpleNamespace(
        role=Role.CODER,
        payload={"task": "do it", "model_override": "override-m"},
    )
    seen: dict = {}

    async def fake_run_agent(**kwargs):
        seen["model"] = kwargs.get("model")
        return SimpleNamespace(content=json.dumps({"summary": "ok", "files": []}), tool_steps=0)

    original = worker_mod.run_agent
    worker_mod.run_agent = fake_run_agent  # type: ignore[assignment]
    try:
        result = asyncio.run(worker._run_impl(envelope, snapshot))  # type: ignore[arg-type]
    finally:
        worker_mod.run_agent = original  # type: ignore[assignment]
    assert result.ok is True
    assert seen["model"] == "override-m"


def test_worker_without_override_keeps_role_default():
    settings = Settings(role="coder", node_name="node-a", model_coder="default-m")
    worker = Worker(settings, bus=FakeBus(), backend=object())  # type: ignore[arg-type]
    snapshot = TaskSnapshot(
        task_id="ovr-2", trace_id="t2", task="do it", state=TaskState.PLANNING
    )
    envelope = SimpleNamespace(role=Role.CODER, payload={"task": "do it"})
    seen: dict = {}

    async def fake_run_agent(**kwargs):
        seen["model"] = kwargs.get("model")
        return SimpleNamespace(content=json.dumps({"summary": "ok", "files": []}), tool_steps=0)

    original = worker_mod.run_agent
    worker_mod.run_agent = fake_run_agent  # type: ignore[assignment]
    try:
        asyncio.run(worker._run_impl(envelope, snapshot))  # type: ignore[arg-type]
    finally:
        worker_mod.run_agent = original  # type: ignore[assignment]
    assert seen["model"] == "default-m"


def test_telegram_retry_command_parses_model_flag():
    import asyncio

    from memnc.config import Settings
    from memnc.telegram import TelegramObserver

    obs = TelegramObserver(Settings(node_name="t", telegram_allow="42"), _FakeBot())  # type: ignore[arg-type]
    captured: list = []

    async def fake_resolve(chat: str, arg: str) -> str:
        return "t1"

    async def fake_control(chat: str, action: str, argument: str, model_override: str = "") -> None:
        captured.append((action, argument, model_override))

    obs._resolve_task = fake_resolve  # type: ignore[method-assign]
    obs._control = fake_control  # type: ignore[method-assign]
    asyncio.run(obs.handle_command("42", "/retry t1 --model big:32b"))
    assert captured == [("retry", "t1", "big:32b")]

    captured.clear()
    asyncio.run(obs.handle_command("42", "/retry t1"))
    assert captured == [("retry", "t1", "")]


def test_control_sends_model_override_arg():
    import asyncio
    import json as _json

    obs, nc = _observer({"ok": True, "task_id": "t1"})
    asyncio.run(obs._control("42", "retry", "t1", model_override="big:32b"))
    assert nc.subjects == ["mesh.api.request.task"]
    body = _json.loads(nc.raw[0])
    assert body["verb"] == "task.retry"
    assert body["args"]["model_override"] == "big:32b"

