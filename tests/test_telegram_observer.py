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


# -- digest filtering ------------------------------------------------------

def _observer():
    observer = TelegramObserver.__new__(TelegramObserver)
    observer.settings = type("S", (), {"telegram_verbose": False})()
    observer._last_state = {}
    observer._announced = set()
    return observer


async def test_digest_suppresses_same_state_chatter():
    obs = _observer()
    assert obs._digest_event(envelope()) is True          # CODING transition
    assert obs._digest_event(envelope(payload={"event": "dispatch", "detail": "cmd", "state": "CODING", "revisions": 1})) is False
    assert obs._digest_event(envelope(node="node-b-coder")) is False  # still CODING


async def test_digest_announces_state_transition():
    obs = _observer()
    obs._digest_event(envelope())
    assert obs._digest_event(envelope(payload={"event": "done", "detail": "", "state": "DONE", "revisions": 2})) is True


async def test_digest_announces_new_failure_detail_once():
    obs = _observer()
    fail = envelope(payload={"event": "failed", "detail": "tool step limit (12) exceeded", "state": "FAILED", "revisions": 0})
    assert obs._digest_event(fail) is True
    assert obs._digest_event(fail) is False               # same detail suppressed
    other = envelope(payload={"event": "failed", "detail": "artifact apply denied: /workspace", "state": "FAILED", "revisions": 0})
    assert obs._digest_event(other) is True               # new failure reason


async def test_digest_verbose_always_broadcasts():
    obs = _observer()
    obs.settings = type("S", (), {"telegram_verbose": True})()
    assert obs._digest_event(envelope()) is True
    assert obs._digest_event(envelope()) is True


def test_digest_ignores_non_task_events():
    obs = _observer()
    assert obs._digest_event({"task_id": "", "payload": {}}) is False
