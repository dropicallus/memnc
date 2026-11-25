"""P5.1 lease by liveness: release_claim CAS + orchestrator reaper triggers."""

from __future__ import annotations

import asyncio
import time

from memnc.bus import Bus
from memnc.config import Settings
from memnc.orchestrator import Orchestrator
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
    bus.kv = FakeKV()  # type: ignore[assignment]
    return bus


def _snapshot(task_id: str, **fields) -> TaskSnapshot:
    return TaskSnapshot(task_id=task_id, trace_id="tr-p51", task="do a thing", **fields)


# -- release_claim -----------------------------------------------------------
def test_release_claim_returns_task_to_serving_role():
    bus = _bus_with_kv()
    snap = _snapshot(
        "r1",
        state=TaskState.CODING,
        claimable=True,
        claimed_by="node-a-coder:coder",
        claimed_at_epoch=time.time(),
    )
    asyncio.run(bus.put_snapshot(snap))

    released = asyncio.run(bus.release_claim("r1", "node silent for 120s"))

    assert released is not None
    assert released.claimed_by == ""
    assert released.claimed_at_epoch == 0.0
    assert released.state is TaskState.CODING
    assert released.next_role == "coder"
    assert released.claimable is True
    assert released.reaps == 1
    assert released.attempt == 0  # infra death must not burn the model budget
    assert any(entry.event == "claim.reaped" for entry in released.history)

    # the freed task is claimable again by the same role
    reclaimed = asyncio.run(bus.try_claim("r1", "node-b-coder:coder", role="coder"))
    assert reclaimed is not None and reclaimed.claimed_by == "node-b-coder:coder"


def test_release_claim_on_unclaimed_task_is_a_noop():
    bus = _bus_with_kv()
    asyncio.run(bus.put_snapshot(_snapshot("r2", state=TaskState.CODING, claimable=True)))
    assert asyncio.run(bus.release_claim("r2", "nothing to do")) is None


def test_release_claim_never_touches_terminal_tasks():
    bus = _bus_with_kv()
    asyncio.run(
        bus.put_snapshot(_snapshot("r3", state=TaskState.DONE, claimed_by="node-a-coder:coder"))
    )
    assert asyncio.run(bus.release_claim("r3", "too late")) is None


def test_release_claim_second_call_is_a_noop():
    """Two reapers racing: the loser sees a released claim and does nothing."""
    bus = _bus_with_kv()
    asyncio.run(
        bus.put_snapshot(
            _snapshot(
                "r4",
                state=TaskState.CRITIQUE,
                claimable=True,
                claimed_by="node-a-critic:critic",
            )
        )
    )
    first = asyncio.run(bus.release_claim("r4", "silent"))
    second = asyncio.run(bus.release_claim("r4", "silent"))
    assert first is not None and first.reaps == 1
    assert second is None


# -- orchestrator liveness ---------------------------------------------------
class ReapBus:
    """Minimal bus surface used by _reap_stale_claims."""

    def __init__(self, snapshots):
        self._snapshots = list(snapshots)
        self.released: list[tuple[str, str]] = []
        self.requeues: list[str] = []
        self.events: list[tuple[str, str]] = []

    async def list_tasks(self):
        return self._snapshots

    async def release_claim(self, task_id, reason, role=None):
        self.released.append((task_id, reason))
        for snap in self._snapshots:
            if snap.task_id == task_id:
                snap.claimed_by = ""
                snap.next_role = "coder"
                snap.state = TaskState.CODING
                snap.claimable = True
                snap.reaps += 1
                return snap
        return None

    async def publish_envelope(self, subject, envelope):
        self.requeues.append(subject)

    async def emit_event(self, snapshot, event, detail=""):
        self.events.append((event, detail))


def _orchestrator(bus) -> Orchestrator:
    settings = Settings(node_name="orchestrator")
    return Orchestrator(settings=settings, bus=bus)  # type: ignore[arg-type]


def test_agent_silence_is_none_for_an_unseen_worker():
    orch = _orchestrator(ReapBus([]))
    assert orch._agent_silence("node-x-coder:coder", time.time()) is None


def test_agent_silence_reads_the_node_part_of_the_claim():
    orch = _orchestrator(ReapBus([]))
    orch._last_seen["node-x-coder"] = 1000.0
    assert orch._agent_silence("node-x-coder:coder", 1075.0) == 75.0


def test_reaper_releases_a_claim_whose_node_went_silent():
    snap = _snapshot(
        "q1",
        state=TaskState.CODING,
        claimable=False,
        claimed_by="node-x-coder:coder",
        claimed_at_epoch=time.time() - 10,  # fresh claim, dead worker
    )
    bus = ReapBus([snap])
    orch = _orchestrator(bus)
    orch._last_seen["node-x-coder"] = time.time() - 300  # silent, i.e. dead

    asyncio.run(orch._reap_stale_claims())

    assert len(bus.released) == 1
    assert "silent for" in bus.released[0][1]
    assert bus.requeues == ["mesh.task.q1.requeue"]
    assert bus.events and bus.events[0][0] == "claim.reaped"


def test_reaper_leaves_a_live_workers_fresh_claim_alone():
    snap = _snapshot(
        "q2",
        state=TaskState.CODING,
        claimed_by="node-x-coder:coder",
        claimed_at_epoch=time.time() - 5,
    )
    bus = ReapBus([snap])
    orch = _orchestrator(bus)
    orch._last_seen["node-x-coder"] = time.time()  # beating

    asyncio.run(orch._reap_stale_claims())

    assert bus.released == []


def test_reaper_falls_back_to_ttl_for_a_never_seen_worker():
    snap = _snapshot(
        "q3",
        state=TaskState.CODING,
        claimed_by="node-ghost-coder:coder",
        claimed_at_epoch=time.time() - 3600,
    )
    bus = ReapBus([snap])
    orch = _orchestrator(bus)

    asyncio.run(orch._reap_stale_claims())

    assert len(bus.released) == 1
    assert "older than" in bus.released[0][1]
