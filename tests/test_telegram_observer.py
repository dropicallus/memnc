"""T1 Telegram observer: pure-logic tests (no network, no NATS)."""

from __future__ import annotations

import asyncio

import pytest

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
        "payload": {
            "event": "claimed",
            "detail": "workspace edit",
            "state": "CODING",
            "revisions": 1,
        },
    }
    base.update(overrides)
    return base


def test_allowed_chats_parses_and_normalizes():
    assert allowed_chats("123, @operator , 456,") == {"123", "operator", "456"}
    assert allowed_chats("") == set()
    assert allowed_chats("  ") == set()


def test_format_event_compact_line():
    line = format_event(envelope())
    assert "#1a1ad82fd5e0" in line
    assert "CODING rev1" in line
    assert "[node-a-coder] claimed" in line
    assert "workspace edit" in line


def test_format_event_truncates_long_detail():
    line = format_event(
        envelope(payload={"event": "e", "detail": "x" * 500, "state": "DONE", "revisions": 0})
    )
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
    observer.settings = type(
        "S", (), {"telegram_verbose": False, "telegram_quiet_progress": False}
    )()
    observer._last_state = {}
    observer._announced = set()
    return observer


async def test_digest_suppresses_same_state_chatter():
    obs = _observer()
    assert obs._digest_event(envelope()) is True  # CODING transition
    assert (
        obs._digest_event(
            envelope(
                payload={"event": "dispatch", "detail": "cmd", "state": "CODING", "revisions": 1}
            )
        )
        is False
    )
    assert obs._digest_event(envelope(node="node-b-coder")) is False  # still CODING


async def test_digest_announces_state_transition():
    obs = _observer()
    obs._digest_event(envelope())
    assert (
        obs._digest_event(
            envelope(payload={"event": "done", "detail": "", "state": "DONE", "revisions": 2})
        )
        is True
    )


async def test_digest_announces_new_failure_detail_once():
    obs = _observer()
    fail = envelope(
        payload={
            "event": "failed",
            "detail": "tool step limit (12) exceeded",
            "state": "FAILED",
            "revisions": 0,
        }
    )
    assert obs._digest_event(fail) is True
    assert obs._digest_event(fail) is False  # same detail suppressed
    other = envelope(
        payload={
            "event": "failed",
            "detail": "artifact apply denied: /workspace",
            "state": "FAILED",
            "revisions": 0,
        }
    )
    assert obs._digest_event(other) is True  # new failure reason


async def test_digest_verbose_always_broadcasts():
    obs = _observer()
    obs.settings = type("S", (), {"telegram_verbose": True})()
    assert obs._digest_event(envelope()) is True
    assert obs._digest_event(envelope()) is True


async def test_digest_quiet_progress_drops_routine_transitions():
    obs = _observer()
    obs.settings = type(
        "S", (), {"telegram_verbose": False, "telegram_quiet_progress": True}
    )()
    assert obs._digest_event(envelope()) is False  # CODING transition dropped
    assert (
        obs._digest_event(
            envelope(payload={"event": "claimed", "detail": "", "state": "CLAIMED"})
        )
        is False
    )
    # terminal and action states still broadcast
    assert (
        obs._digest_event(
            envelope(payload={"event": "done", "detail": "", "state": "DONE"})
        )
        is True
    )
    wait = envelope(
        payload={"event": "wait_input", "detail": "needs human", "state": "WAIT_INPUT"}
    )
    assert obs._digest_event(wait) is True


async def test_digest_quiet_progress_still_tracks_state():
    obs = _observer()
    obs.settings = type(
        "S", (), {"telegram_verbose": False, "telegram_quiet_progress": True}
    )()
    obs._digest_event(envelope())  # CODING, suppressed
    tid = envelope()["task_id"]
    assert obs._last_state.get(tid) == "CODING"


def test_digest_ignores_non_task_events():
    obs = _observer()
    assert obs._digest_event({"task_id": "", "payload": {}}) is False


# -- status digest (14.11 P0) -------------------------------------------------


def test_format_status_full():
    from memnc.observations import TaskObservation
    from memnc.telegram import format_status

    snapshots = [
        {
            "task_id": "1a1aaaaa",
            "state": "CODING",
            "claimed_by": "node-b-coder",
            "task": "write docs",
        },
        {"task_id": "1a1bbbbb", "state": "DONE", "claimed_by": "", "task": "x"},
        {"task_id": "1a1ccccc", "state": "FAILED", "claimed_by": "", "task": "y"},
    ]
    nodes = [{"node_id": "node-a", "roles": ["coder"], "models": ["m"]}]
    obs = TaskObservation(
        task_id="1a1ccccc",
        trace_id="t",
        task="y",
        outcome="failed",
        route={"coder": "node-a:m"},
    )
    out = format_status(snapshots, nodes, [], [obs])
    assert "3 tasks:" in out
    assert "DONE=1" in out
    assert "FAILED=1" in out
    assert "CODING=1" in out
    assert "active: #1a1aaaaa CODING (node-b-coder)" in out
    assert "nodes: node-a" in out
    assert "model requests: none pending" in out
    assert "recent failures:" in out
    assert "1a1ccccc failed" in out


def test_format_status_empty_mesh():
    from memnc.telegram import format_status

    out = format_status([], [], [], [])
    assert "0 tasks:" in out
    assert "active: none" in out
    assert "nodes: none (mesh empty)" in out
    assert "recent failures: none" in out


async def test_on_event_failed_broadcasts_card_with_retry_hint():

    obs = _observer()
    sent = []

    async def fake_broadcast(text, markup=None):
        sent.append((text, markup))

    async def fake_snapshot(task_id):
        class S:
            task = "Write docs/p2b_design.md:\nmore text"

            def model_dump(self):
                return {"task": self.task}

        return S()

    obs.broadcast = fake_broadcast
    obs._seen_tasks = set()
    obs.bus = type("B", (), {"get_snapshot": staticmethod(fake_snapshot)})()
    env = envelope(
        payload={
            "event": "failed",
            "detail": "permission denied",
            "state": "FAILED",
            "revisions": 1,
        }
    )
    msg = type("M", (), {"data": __import__("json").dumps(env).encode()})()
    await obs.on_event(msg)
    card, markup = sent[-1]
    assert "FAILED #1a1ad82fd5e" in card
    assert "cause: permission denied" in card
    assert "/retry 1a1ad82fd5e07764bfe0756" in card
    # 14.11 P0 slice 1: failure cards carry [Retry][Cancel] buttons
    from memnc.telegram import task_buttons

    assert markup == task_buttons("1a1ad82fd5e07764bfe0756")




# -- roster updates gated behind telegram_roster_updates --------


class _RosterNode:
    def __init__(self, node_id):
        self.node_id = node_id


class _RosterBus:
    """list_nodes returns the first payload, then stops the poll loop."""

    def __init__(self, node_ids):
        self.calls = 0
        self.node_ids = node_ids

    async def connect(self):
        pass

    async def list_nodes(self):
        self.calls += 1
        if self.calls > 1:
            raise asyncio.CancelledError  # stops the poll loop in tests
        return [_RosterNode(n) for n in self.node_ids]


def _roster_observer(node_ids, roster_updates, seen_first=False):
    obs = TelegramObserver.__new__(TelegramObserver)
    obs.settings = type(
        "S",
        (),
        {"telegram_verbose": False, "telegram_roster_updates": roster_updates, "telegram_poll_s": 5},
    )()
    obs.bus = _RosterBus(node_ids)
    obs.sent = []

    async def broadcast(text, **kwargs):
        obs.sent.append(text)

    obs.broadcast = broadcast
    obs._seen_nodes = {"node-a"} if seen_first else set()
    return obs


async def test_roster_churn_suppressed_by_default():
    obs = _roster_observer(["node-a", "node-a-coder"], False, seen_first=True)
    with pytest.raises(asyncio.CancelledError):
        await obs.run_roster()
    assert obs.sent == []  # churn stays silent without the flag


async def test_roster_churn_broadcast_when_enabled():
    obs = _roster_observer(["node-a", "node-a-coder"], True, seen_first=True)
    with pytest.raises(asyncio.CancelledError):
        await obs.run_roster()
    assert obs.sent == ["mesh: joined: node-a-coder"]


async def test_roster_first_poll_is_silent_even_when_enabled():
    obs = _roster_observer(["node-a", "node-a-coder"], True, seen_first=False)
    with pytest.raises(asyncio.CancelledError):
        await obs.run_roster()
    assert obs.sent == []  # initial snapshot is not announced as churn


# -- pending-request push (14.11 P0 remnant) ---------------------------------


class _ReqBus:
    def __init__(self, requests):
        self._requests = requests

    async def list_model_requests(self):
        return self._requests


def _req_observer(requests, flag):
    from memnc.schema import ModelRequest  # noqa: F401  (typing helper below)

    obs = TelegramObserver.__new__(TelegramObserver)
    obs.settings = type("S", (), {"telegram_pending_requests": flag})()
    obs.bus = _ReqBus(requests)
    obs.chats = {"chat1"}
    obs.sent = []

    async def broadcast(text, markup=None):
        obs.sent.append((text, markup))

    obs.broadcast = broadcast
    obs._seen_requests = set()
    return obs


def _mreq(request_id, status="pending"):
    from memnc.schema import ModelRequest

    return ModelRequest(
        request_id=request_id,
        model="m8b",
        task_id="t1",
        status=status,
        reason="unmet needs.model",
    )


async def test_pending_request_pushed_once_with_buttons():
    obs = _req_observer([_mreq("mr1")], True)
    await obs._push_pending_requests()
    await obs._push_pending_requests()
    assert len(obs.sent) == 1
    text, markup = obs.sent[0]
    assert "mr1" in text and "m8b" in text
    assert markup["inline_keyboard"][0][0]["callback_data"] == "mr:mr1:approve"


async def test_pending_push_disabled_by_default():
    obs = _req_observer([_mreq("mr1")], False)
    await obs._push_pending_requests()
    assert obs.sent == []


async def test_decided_requests_not_pushed():
    obs = _req_observer([_mreq("mr1", "approved")], True)
    await obs._push_pending_requests()
    assert obs.sent == []


def test_format_pending_request_card():
    from memnc.telegram import format_pending_request

    text = format_pending_request(_mreq("mr1"))
    assert "mr1" in text and "m8b" in text
    assert "t1" in text and "unmet needs.model" in text
