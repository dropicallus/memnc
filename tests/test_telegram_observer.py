"""T1 Telegram observer: pure-logic tests (no network, no NATS)."""

from __future__ import annotations

from memnc.telegram import TelegramObserver, allowed_chats, format_event, format_task


def envelope(**overrides):
    base = {
        "task_id": "1a1ad82fd5e07764bfe0756",
        "trace_id": "t1",
        "kind": "event",
        "node": "node-a-coder",
        "state": "CODING",
        "attempt": 0,
        "ts": 1760000000,
        "payload": {"event": "claimed", "detail": "workspace edit", "state": "CODING", "revisions": 1},
    }
    base.update(overrides)
    return base


def test_allowed_chats_parses_and_normalizes():
    assert allowed_chats("123, @frank , 456,") == {"123", "frank", "456"}
    assert allowed_chats("") == set()
    assert allowed_chats("  ") == set()


def test_format_event_compact_line():
    line = format_event(envelope())
    assert "#1a1ad82fd5e0" in line
    assert "CODING rev1" in line
    assert "[node-a-coder] claimed" in line
    assert "workspace edit" in line


def test_format_event_truncates_long_detail():
    line = format_event(envelope(payload={"event": "e", "detail": "x" * 500, "state": "DONE", "revisions": 0}))
    assert len(line) < 400


def test_format_task_includes_key_fields():
    text = format_task(
        {
            "task_id": "abc123",
            "state": "DONE",
            "claimed_by": "node-a-critic:critic",
            "revisions": 2,
            "attempt": 1,
            "prompt": "Create hello.py",
            "critic_verdict": "fulfilled",
        }
    )
    assert "#abc123 DONE rev2 attempt1" in text
    assert "node-a-critic:critic" in text
    assert "Create hello.py" in text
    assert "fulfilled" in text


def test_format_task_handles_unclaimed_and_missing():
    text = format_task({"task_id": "x", "state": "RECEIVED"})
    assert "unclaimed" in text


class _RecordingBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat, text):
        self.sent.append((chat, text))


async def test_broadcast_goes_only_to_allowlist():
    settings = type("S", (), {"telegram_allow": "111,222"})()
    bot = _RecordingBot()
    observer = TelegramObserver.__new__(TelegramObserver)
    observer.bot = bot
    observer.chats = allowed_chats(settings.telegram_allow)
    await observer.broadcast("hello")
    assert {c for c, _ in bot.sent} == {"111", "222"}


async def test_broadcast_swallows_delivery_errors():
    class Failing(_RecordingBot):
        async def send_message(self, chat, text):
            raise RuntimeError("down")

    bot = Failing()
    observer = TelegramObserver.__new__(TelegramObserver)
    observer.bot = bot
    observer.chats = {"111"}
    await observer.broadcast("hello")  # must not raise
    assert bot.sent == []
