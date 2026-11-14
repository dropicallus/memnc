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


# -- P1 item 7: command discovery (/help groups + BotFather registration) ----


def test_format_help_groups_commands():
    from memnc.telegram import format_help

    out = format_help()
    assert out.startswith("commands:")
    for group in ("view:", "act:", "decide:"):
        assert group in out
    assert "/retry <task-id> [--model <name>]" in out
    assert "submit" in out


def test_botfather_commands_flattens_and_skips_two_word():
    from memnc.telegram import botfather_commands

    cmds = botfather_commands()
    names = [c["command"] for c in cmds]
    assert "submit" in names and "retry" in names
    assert all(" " not in n for n in names)
    assert all(c["description"] for c in cmds)


def test_set_my_commands_posts_payload():
    import asyncio

    from memnc.config import Settings
    from memnc.telegram import BotAPI, TelegramObserver, botfather_commands

    posted: list = []

    class _Resp:
        def raise_for_status(self) -> None:
            pass

    class _FakeClient:
        async def post(self, url: str, json=None):  # noqa: A002
            posted.append((url, json))
            return _Resp()

    api = BotAPI("t0k3n")
    api._client = _FakeClient()
    asyncio.run(api.set_my_commands(botfather_commands()))
    url, payload = posted[0]
    assert url.endswith("/bott0k3n/setMyCommands")
    assert payload == {"commands": botfather_commands()}

    # Startup registration is best-effort: a failing bot must not crash run().
    class _LocalBot:
        token = "t0k3n"

    obs = TelegramObserver(Settings(node_name="t", telegram_allow="42"), _LocalBot())  # type: ignore[arg-type]

    async def boom(commands):
        raise RuntimeError("down")

    async def noop():
        pass

    obs.bot.set_my_commands = boom  # type: ignore[method-assign]
    obs.run_events = noop  # type: ignore[method-assign]
    obs.run_commands = noop  # type: ignore[method-assign]
    obs.run_roster = noop  # type: ignore[method-assign]
    asyncio.run(obs.run())  # must not raise


# -- P1 item 5: completion cards deliver the result --------------------------


def test_format_done_card_carries_result_and_files():
    from memnc.telegram import format_done_card

    snap = {
        "task_id": "abcd12345678",
        "task": "write the thing\nmore lines",
        "code": {
            "summary": "did the thing",
            "files": [{"path": "out/x.py"}, {"path": "out/y.py"}, {}],
        },
    }
    card = format_done_card(snap)
    assert card.startswith("DONE #abcd1234567")
    assert "write the thing" in card
    assert "result: did the thing" in card
    assert "files: out/x.py, out/y.py" in card


def test_format_done_card_minimal():
    from memnc.telegram import format_done_card

    card = format_done_card({"task_id": "z", "task": "", "code": None})
    assert card == "DONE #z"


def test_done_event_broadcasts_result_card():
    import asyncio
    import json
    from types import SimpleNamespace

    from memnc.config import Settings
    from memnc.telegram import TelegramObserver

    class _Bus:
        def __init__(self, snap) -> None:
            self._snap = snap

        async def get_snapshot(self, task_id: str):
            return self._snap

    code = SimpleNamespace(
        model_dump=lambda: {
            "summary": "it worked",
            "files": [{"path": "a.py"}],
        }
    )
    snap = SimpleNamespace(
        model_dump=lambda: {
            "task_id": "t-done-1",
            "task": "do work",
            "code": code.model_dump(),
        }
    )
    obs = TelegramObserver(Settings(node_name="t", telegram_allow="42"), _FakeBot())  # type: ignore[arg-type]
    obs.bus = _Bus(snap)  # type: ignore[assignment]

    envelope = {"task_id": "t-done-1", "state": "DONE", "payload": {"state": "done"}}
    asyncio.run(obs.on_event(SimpleNamespace(data=json.dumps(envelope).encode())))
    sent = obs.bot.sent
    done = [t for _c, t in sent if t.startswith("DONE #")]
    assert done and "result: it worked" in done[0]
    assert "files: a.py" in done[0]

