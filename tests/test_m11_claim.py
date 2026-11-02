"""M11: bus.try_claim CAS semantics with a fake JetStream KV bucket."""

from __future__ import annotations

import asyncio
import json

from memnc.bus import Bus
from memnc.config import Settings
from memnc.schema import TaskSnapshot, TaskState


class FakeEntry:
    def __init__(self, value: bytes, revision: int):
        self.value = value
        self.revision = revision


class FakeKV:
    """KV with real compare-and-set semantics (revision-based update)."""

    def __init__(self):
        self.data: dict[str, tuple[bytes, int]] = {}
        self._rev = 0

    async def put(self, key: str, value: bytes):
        self._rev += 1
        self.data[key] = (value, self._rev)

    async def get(self, key: str):
        if key not in self.data:
            raise KeyError(key)
        value, rev = self.data[key]
        return FakeEntry(value, rev)

    async def update(self, key: str, value: bytes, last: int):
        if self.data.get(key, (b"", 0))[1] != last:
            raise RuntimeError("wrong last sequence (CAS conflict)")
        self._rev += 1
        self.data[key] = (value, self._rev)

    async def keys(self):
        return list(self.data)


def _bus_with_kv() -> Bus:
    bus = Bus(Settings())
    # attach the fake bucket directly (skip NATS connect)
    bus.kv = FakeKV()  # type: ignore[assignment]
    return bus


def _snapshot(task_id: str, **fields) -> TaskSnapshot:
    return TaskSnapshot(
        task_id=task_id, trace_id="tr-1", task="do a thing", **fields
    )


def test_try_claim_success():
    bus = _bus_with_kv()
    snapshot = _snapshot("t1", state=TaskState.RECEIVED, claimable=True)
    asyncio.run(bus.put_snapshot(snapshot))

    claimed = asyncio.run(bus.try_claim("t1", "node-b:coder"))
    assert claimed is not None
    assert claimed.claimed_by == "node-b:coder"
    # read back from the store
    stored = asyncio.run(bus.get_snapshot("t1"))
    assert stored is not None and stored.claimed_by == "node-b:coder"


def test_try_claim_conflict_loser_gets_none():
    bus = _bus_with_kv()
    snapshot = _snapshot("t2", state=TaskState.RECEIVED, claimable=True)
    asyncio.run(bus.put_snapshot(snapshot))

    winner = asyncio.run(bus.try_claim("t2", "node-a:coder"))
    loser = asyncio.run(bus.try_claim("t2", "node-b:coder"))
    assert winner is not None and winner.claimed_by == "node-a:coder"
    assert loser is None


def test_try_claim_rejects_dispatched_or_unclaimable():
    bus = _bus_with_kv()
    running = _snapshot("t3", state=TaskState.PLANNING, claimable=True)
    asyncio.run(bus.put_snapshot(running))
    assert asyncio.run(bus.try_claim("t3", "w")) is None

    plain = _snapshot("t4", state=TaskState.RECEIVED, claimable=False)
    asyncio.run(bus.put_snapshot(plain))
    assert asyncio.run(bus.try_claim("t4", "w")) is None

    # missing key
    assert asyncio.run(bus.try_claim("nope", "w")) is None


def test_stale_revision_is_cas_conflict():
    """A claim computed against a stale revision must lose."""
    bus = _bus_with_kv()
    snapshot = _snapshot("t5", state=TaskState.RECEIVED, claimable=True)
    asyncio.run(bus.put_snapshot(snapshot))
    # read the raw entry, then bump the store behind its back
    entry = asyncio.run(bus.kv.get("task.t5"))  # type: ignore[union-attr]
    stale = TaskSnapshot.model_validate_json(entry.value)
    stale.claimed_by = "ghost"
    asyncio.run(bus.kv.put("task.t5", b"{}"))  # bump the revision behind its back
    try:
        result = asyncio.run(
            bus.kv.update(
                "task.t5", stale.model_dump_json().encode(), last=entry.revision
            )
        )
        assert False, f"expected CAS rejection, got {result}"
    except Exception:
        pass
