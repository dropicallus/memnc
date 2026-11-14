"""14.11 P0 slice 1: short task-id prefixes + inline keyboards (pure logic)."""

from __future__ import annotations

import asyncio

from memnc.config import Settings
from memnc.schema import TaskSnapshot
from memnc.telegram import (
    MIN_TASK_PREFIX,
    TelegramObserver,
    mreq_buttons,
    parse_callback,
    resolve_task_id,
    task_buttons,
)

SNAP_A = "abcdef1234567890"
SNAP_B = "bcdef1234567890a"


class _FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple] = []
        self.answered: list[str] = []
        self.edited: list[tuple] = []

    async def send_message(self, chat: str, text: str, markup=None) -> None:
        self.sent.append((chat, text, markup))

    async def answer_callback(self, callback_query_id: str, text: str = "") -> None:
        self.answered.append(callback_query_id)

    async def edit_message_text(self, chat: str, message_id: int, text: str) -> None:
        self.edited.append((chat, message_id, text))


class _FakeBus:
    def __init__(self, snapshots):
        self.snapshots = snapshots

    async def list_tasks(self):
        return self.snapshots

    async def get_snapshot(self, task_id: str):
        for s in self.snapshots:
            if s.task_id == task_id:
                return s
        return None


def _snap(task_id: str) -> TaskSnapshot:
    return TaskSnapshot(task_id=task_id, trace_id="t1", task="do things")


def _observer(bus) -> TelegramObserver:
    obs = TelegramObserver.__new__(TelegramObserver)
    obs.bus = bus
    obs.bot = _FakeBot()
    obs.chats = {"42"}
    obs.settings = type("S", (), {"telegram_verbose": False})()
    obs._last_state = {}
    obs._announced = set()
    return obs


# -- resolve_task_id --------------------------------------------------------


def test_resolve_task_id_exact_full_id_wins():
    snaps = [_snap(SNAP_A), _snap(SNAP_B)]
    assert resolve_task_id(SNAP_A, [s.model_dump() for s in snaps]) == (SNAP_A, [])
    assert resolve_task_id("#" + SNAP_A, [s.model_dump() for s in snaps]) == (SNAP_A, [])


def test_resolve_task_id_unique_prefix_resolves():
    snaps = [_snap(SNAP_A), _snap(SNAP_B)]
    assert resolve_task_id("abcdef", [s.model_dump() for s in snaps]) == (SNAP_A, [])


def test_resolve_task_id_ambiguous_returns_candidates():
    a, b = "aabbcc11223344", "aabbcc55667788"
    snaps = [_snap(a), _snap(b)]
    tid, cands = resolve_task_id("aabbcc", [s.model_dump() for s in snaps])
    assert tid == ""
    assert cands == sorted([a, b])


def test_resolve_task_id_short_prefix_without_exact_id_is_refused():
    snaps = [_snap(SNAP_A), _snap(SNAP_B)]
    assert resolve_task_id("abc", [s.model_dump() for s in snaps]) == ("", [])
    assert MIN_TASK_PREFIX == 6


def test_resolve_task_id_unknown_prefix():
    snaps = [_snap(SNAP_A)]
    assert resolve_task_id("zzzzzz", [s.model_dump() for s in snaps]) == ("", [])
    assert resolve_task_id("", [s.model_dump() for s in snaps]) == ("", [])


# -- buttons + callback parsing ---------------------------------------------


def test_task_buttons_shape():
    kb = task_buttons(SNAP_A)
    row = kb["inline_keyboard"][0]
    assert [b["text"] for b in row] == ["Retry", "Cancel"]
    assert [b["callback_data"] for b in row] == [
        f"tk:{SNAP_A}:retry",
        f"tk:{SNAP_A}:cancel",
    ]


def test_mreq_buttons_shape():
    kb = mreq_buttons("req-1")
    row = kb["inline_keyboard"][0]
    assert [b["text"] for b in row] == ["Approve", "Reject"]


def test_parse_callback_accepts_known_kinds():
    assert parse_callback("tk:id1:retry") == ("task", "id1", "retry")
    assert parse_callback("tk:id1:cancel") == ("task", "id1", "cancel")
    assert parse_callback("mr:req-1:approve") == ("mreq", "req-1", "approve")
    assert parse_callback("mr:req-1:reject") == ("mreq", "req-1", "reject")
    assert parse_callback("nd:node-a-coder:pause") == ("node", "node-a-coder", "pause")
    assert parse_callback("nd:node-a-coder:resume") == ("node", "node-a-coder", "resume")


def test_parse_callback_rejects_malformed_and_unknown():
    assert parse_callback("tk:id1:delete") is None
    assert parse_callback("tk:id1") is None
    assert parse_callback("tk:id1:x:y") is None
    assert parse_callback("tk::retry") is None
    assert parse_callback("xx:id1:retry") is None
    assert parse_callback("") is None


# -- commands: prefix resolution -------------------------------------------


def test_state_command_resolves_prefix():
    obs = _observer(_FakeBus([_snap(SNAP_A)]))
    asyncio.run(obs.handle_command("42", "/state abcdef"))
    assert obs.bot.sent and "abcdef" in obs.bot.sent[-1][1]


def test_retry_command_resolves_prefix():
    obs = _observer(_FakeBus([_snap(SNAP_A)]))
    calls: list[tuple] = []

    async def fake_control(chat, action, argument, model_override: str = ""):
        calls.append((chat, action, argument))

    obs._control = fake_control  # type: ignore[method-assign]
    asyncio.run(obs.handle_command("42", "/retry abcdef"))
    assert calls == [("42", "retry", SNAP_A)]


def test_retry_ambiguous_prefix_sends_no_control_call():
    obs = _observer(_FakeBus([_snap("aabbcc11223344"), _snap("aabbcc55667788")]))
    calls: list[tuple] = []

    async def fake_control(chat, action, argument, model_override: str = ""):
        calls.append((chat, action, argument))

    obs._control = fake_control  # type: ignore[method-assign]
    asyncio.run(obs.handle_command("42", "/retry aabbcc"))
    assert calls == []
    assert obs.bot.sent[-1][1].startswith("ambiguous prefix")


def test_retry_short_prefix_is_refused():
    obs = _observer(_FakeBus([_snap(SNAP_A)]))
    calls: list[tuple] = []

    async def fake_control(chat, action, argument, model_override: str = ""):
        calls.append((chat, action, argument))

    obs._control = fake_control  # type: ignore[method-assign]
    asyncio.run(obs.handle_command("42", "/retry abc"))
    assert calls == []
    assert "at least 6" in obs.bot.sent[-1][1]


def test_retry_unknown_prefix_reports_missing():
    obs = _observer(_FakeBus([_snap(SNAP_A)]))
    asyncio.run(obs.handle_command("42", "/retry zzzzzz"))
    assert obs.bot.sent[-1][1] == "task not found: zzzzzz"


def test_gate_command_resolves_prefix():
    obs = _observer(_FakeBus([_snap(SNAP_A)]))
    calls: list[tuple] = []

    async def fake_control(chat, action, argument, model_override: str = ""):
        calls.append((chat, action, argument))

    obs._control = fake_control  # type: ignore[method-assign]
    asyncio.run(obs.handle_command("42", "/gate approve abcdef"))
    assert calls == [("42", "decide", f"{SNAP_A} approve")]


# -- callbacks --------------------------------------------------------------


def test_callback_task_retry_routes_to_control_and_edits_card():
    obs = _observer(_FakeBus([]))
    calls: list[tuple] = []

    async def fake_control(chat, action, argument, model_override: str = ""):
        calls.append((chat, action, argument))

    obs._control = fake_control  # type: ignore[method-assign]
    asyncio.run(obs.handle_callback("42", 7, f"tk:{SNAP_A}:retry"))
    assert calls == [("42", "retry", SNAP_A)]
    assert obs.bot.edited == [("42", 7, f"tk:{SNAP_A}:retry -> sent")]


def test_callback_task_cancel_routes_to_control():
    obs = _observer(_FakeBus([]))
    calls: list[tuple] = []

    async def fake_control(chat, action, argument, model_override: str = ""):
        calls.append((chat, action, argument))

    obs._control = fake_control  # type: ignore[method-assign]
    asyncio.run(obs.handle_callback("42", None, f"tk:{SNAP_A}:cancel"))
    assert calls == [("42", "cancel", SNAP_A)]
    assert obs.bot.edited == []  # no message id -> no edit attempt


def test_callback_mreq_routes_to_decide():
    obs = _observer(_FakeBus([]))
    calls: list[tuple] = []

    async def fake_decide(chat, verdict, request_id):
        calls.append((chat, verdict, request_id))

    obs._decide = fake_decide  # type: ignore[method-assign]
    asyncio.run(obs.handle_callback("42", 3, "mr:req-1:reject"))
    assert calls == [("42", "reject", "req-1")]


def test_callback_node_pause_resume_routes_to_control():
    obs = _observer(_FakeBus([]))
    calls: list[tuple] = []

    async def fake_control(chat, action, argument, model_override: str = ""):
        calls.append((chat, action, argument))

    obs._control = fake_control  # type: ignore[method-assign]
    asyncio.run(obs.handle_callback("42", None, "nd:node-a-coder:pause"))
    asyncio.run(obs.handle_callback("42", None, "nd:node-a-coder:resume"))
    assert calls == [
        ("42", "pause_node", "node-a-coder"),
        ("42", "resume_node", "node-a-coder"),
    ]


def test_callback_unknown_action_is_surfaced():
    obs = _observer(_FakeBus([]))
    asyncio.run(obs.handle_callback("42", None, "tk:id1:delete"))
    assert obs.bot.sent == [("42", "unknown button action", None)]


def test_run_callback_unauthorized_chat_only_answers():
    obs = _observer(_FakeBus([]))
    calls: list[tuple] = []

    async def fake_control(chat, action, argument, model_override: str = ""):
        calls.append((chat, action, argument))

    obs._control = fake_control  # type: ignore[method-assign]
    cq = {
        "id": "cq1",
        "data": "tk:id1:retry",
        "message": {"message_id": 5, "chat": {"id": 99}},
    }
    asyncio.run(obs._run_callback(cq))  # type: ignore[arg-type]
    assert obs.bot.answered == ["cq1"]
    assert calls == []
    assert obs.bot.sent == []


def test_run_callback_authorized_press_end_to_end():
    obs = _observer(_FakeBus([]))
    calls: list[tuple] = []

    async def fake_control(chat, action, argument, model_override: str = ""):
        calls.append((chat, action, argument))

    obs._control = fake_control  # type: ignore[method-assign]
    cq = {
        "id": "cq2",
        "data": f"tk:{SNAP_A}:cancel",
        "message": {"message_id": 9, "chat": {"id": 42}},
    }
    asyncio.run(obs._run_callback(cq))  # type: ignore[arg-type]
    assert obs.bot.answered == ["cq2"]
    assert calls == [("42", "cancel", SNAP_A)]
    assert obs.bot.edited == [("42", 9, f"tk:{SNAP_A}:cancel -> sent")]


# -- broadcast carries markup ------------------------------------------------


def test_broadcast_passes_markup_to_bot():
    obs = _observer(_FakeBus([]))
    kb = task_buttons(SNAP_A)
    asyncio.run(obs.broadcast("boom", markup=kb))
    assert obs.bot.sent == [("42", "boom", kb)]


def test_broadcast_without_markup_keeps_old_signature():
    obs = _observer(_FakeBus([]))
    asyncio.run(obs.broadcast("plain"))
    assert obs.bot.sent == [("42", "plain", None)]


def test_settings_no_new_required_fields():
    s = Settings(node_name="t")
    assert s.node_name == "t"
