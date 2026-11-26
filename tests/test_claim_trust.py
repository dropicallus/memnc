import asyncio
from types import SimpleNamespace

from memnc.bus import Bus
from memnc.schema import TaskSnapshot, TaskState


class _Entry:
    def __init__(self, value: bytes, revision: int) -> None:
        self.value = value
        self.revision = revision


class _KV:
    def __init__(self, snapshot: TaskSnapshot) -> None:
        self.entry = _Entry(snapshot.model_dump_json().encode(), 1)

    async def get(self, key):
        return self.entry

    async def update(self, key, value, last):
        self.entry = _Entry(value, last + 1)


def _claimable() -> TaskSnapshot:
    return TaskSnapshot(
        task_id="t-claim", trace_id="tr", task="x", state=TaskState.RECEIVED, claimable=True
    )


def _bus(granted: bool, credits: list) -> Bus:
    bus = Bus.__new__(Bus)
    bus.kv = _KV(_claimable())

    async def _noop():
        return None

    async def _revocation_of(node_id):
        return None

    async def _grant_read(agent_id):
        return SimpleNamespace() if granted else None

    async def _trust_credit(subject, rating, attribution=None):
        credits.append(subject)

    bus.ensure_trust = _noop  # type: ignore[method-assign]
    bus.ensure_grants = _noop  # type: ignore[method-assign]
    bus.revocation_of = _revocation_of  # type: ignore[method-assign]
    bus.grant_read = _grant_read  # type: ignore[method-assign]
    bus.trust_credit = _trust_credit  # type: ignore[method-assign]
    return bus


def test_claim_earns_trust_only_for_a_granted_worker():
    credits: list = []
    claimed = asyncio.run(_bus(True, credits).try_claim("t-claim", "node-b:coder"))
    assert claimed is not None and claimed.claimed_by == "node-b:coder"
    assert credits == ["node-b:coder"]


def test_claim_by_an_ungranted_name_earns_no_trust():
    credits: list = []
    claimed = asyncio.run(_bus(False, credits).try_claim("t-claim", "node-x:coder"))
    assert claimed is not None
    assert credits == []
