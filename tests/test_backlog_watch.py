"""Backlog watcher terminal events: only task.* finalize, never state.*."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from memnc.cli import _TERMINAL_EVENTS, _backlog_watch


def test_state_events_are_not_terminal() -> None:
    assert "state.failed" not in _TERMINAL_EVENTS
    assert "state.done" not in _TERMINAL_EVENTS
    assert "task.done" in _TERMINAL_EVENTS


def _subscription(events: list[str]):
    class Sub:
        def __init__(self) -> None:
            self._queue = list(events)

        async def next_msg(self, timeout: float):
            if not self._queue:
                raise TimeoutError("no more events")
            msg = MagicMock()
            import json as _json

            msg.data = _json.dumps({"payload": {"event": self._queue.pop(0)}}).encode()
            return msg

        async def unsubscribe(self) -> None:
            pass

    return Sub()


def test_watcher_ignores_state_failed_then_sees_done() -> None:
    events = ["state.planning", "state.failed", "state.coding", "task.done"]
    client = MagicMock()
    client.jetstream = MagicMock(
        return_value=MagicMock(subscribe=AsyncMock(return_value=_subscription(events)))
    )

    async def run() -> str:
        return await _backlog_watch(client, "t1")

    assert asyncio.run(run()) == "task.done"


def test_watcher_still_fails_on_final_task_failed() -> None:
    events = ["state.coding", "task.failed"]
    client = MagicMock()
    client.jetstream = MagicMock(
        return_value=MagicMock(subscribe=AsyncMock(return_value=_subscription(events)))
    )

    async def run() -> str:
        return await _backlog_watch(client, "t1")

    assert asyncio.run(run()) == "task.failed"
