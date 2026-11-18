"""14.11 item 11 (drafted submit): /submit with no argument starts a guided
prompt; plain chat text stays non-commands unless a draft is armed."""

import asyncio
import json

from memnc.config import Settings
from memnc.telegram import TelegramObserver


class _FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict | None]] = []

    async def send_message(
        self, chat: str, text: str, markup: dict | None = None
    ) -> None:
        self.sent.append((chat, text, markup))


class _FakeNc:
    def __init__(self, reply: dict) -> None:
        self.reply = reply
        self.subjects: list[str] = []
        self.raw: list[bytes] = []

    async def request(self, subject: str, payload: bytes, timeout: float = 5):
        self.subjects.append(subject)
        self.raw.append(payload)
        return type("M", (), {"data": json.dumps(self.reply).encode()})()


def _observer(reply: dict | None = None):
    nc = _FakeNc(reply if reply is not None else {"ok": True, "task_id": "t9"})
    obs = TelegramObserver(Settings(node_name="t", telegram_allow="42"), _FakeBot())
    obs.bus.nc = nc
    return obs, nc


def test_submit_without_argument_arms_guided_prompt():
    obs, nc = _observer()
    asyncio.run(obs.handle_command("42", "/submit"))
    assert nc.subjects == []  # nothing submitted yet
    assert "42" in obs._pending_submit
    chat, text, markup = obs.bot.sent[0]
    assert chat == "42"
    assert "task text" in text.lower()
    assert markup is not None and markup.get("force_reply") is True


def test_plain_text_after_arm_becomes_task_and_disarms():
    obs, nc = _observer()
    asyncio.run(obs.handle_command("42", "/submit"))
    asyncio.run(obs.handle_command("42", "audit the mesh roster"))
    assert nc.subjects == ["mesh.api.request.task"]
    args = json.loads(nc.raw[0])["args"]
    assert args == {"agent_id": "telegram-42", "task": "audit the mesh roster"}
    assert "42" not in obs._pending_submit
    assert obs.bot.sent[-1][1] == "submitted: #t9"


def test_plain_text_without_arm_is_ignored():
    obs, nc = _observer()
    asyncio.run(obs.handle_command("42", "just chatting"))
    assert nc.subjects == []
    assert obs.bot.sent == []


def test_command_while_armed_aborts_draft_but_stays_a_command():
    obs, nc = _observer()
    asyncio.run(obs.handle_command("42", "/submit"))
    asyncio.run(obs.handle_command("42", "/help"))
    # the command ran (help text), the draft is gone, nothing was submitted
    assert nc.subjects == []
    assert "42" not in obs._pending_submit
    assert obs.bot.sent[-1][1].startswith("commands:")


def test_direct_submit_with_argument_still_works():
    obs, nc = _observer()
    asyncio.run(obs.handle_command("42", "/submit audit the mesh"))
    assert nc.subjects == ["mesh.api.request.task"]
    args = json.loads(nc.raw[0])["args"]
    assert args["task"] == "audit the mesh"
    assert "42" not in obs._pending_submit
