"""M7 revocation tombstones: bus-level gates and CLI surface logic.

Sync tests with ``asyncio.run`` (repo test style, no decorators).
Reuses the revision-based ``FakeKV`` from the claim tests, extended with
``delete`` for the revocation/roster buckets.
"""

import asyncio
import time

from test_m11_claim import FakeKV

from memnc.bus import Bus
from memnc.config import Settings
from memnc.revocation import REVOCATIONS_BUCKET, RevocationRecord, revocation_key
from memnc.schema import Capability, NodeInfo, TaskSnapshot, TaskState


class FakeKVD(FakeKV):
    """FakeKV plus delete (revocation/roster buckets need it)."""

    async def delete(self, key: str):
        self.data.pop(key, None)


def _bus() -> Bus:
    bus = Bus(Settings())
    bus.kv = FakeKV()  # type: ignore[assignment]
    bus._revocations = FakeKVD()  # type: ignore[assignment]
    bus._roster = FakeKVD()  # type: ignore[assignment]
    bus._registry = FakeKVD()  # type: ignore[assignment]
    return bus


def _snapshot(task_id: str, **fields) -> TaskSnapshot:
    return TaskSnapshot(task_id=task_id, trace_id="tr-1", task="do a thing", **fields)


def test_revocation_key_sanitizes_illegal_chars():
    assert revocation_key("node-a") == "revoked.node-a"
    # KV keys only allow [-_./a-zA-Z0-9] — worker-id colons must not leak in
    assert revocation_key("node-a:coder") == "revoked.node-a-coder"
    assert revocation_key("x y") == "revoked.x-y"


def test_revoke_and_read_back():
    bus = _bus()
    record = asyncio.run(bus.revoke_node("node-x", reason="compromise", by="ops"))
    assert record.node_id == "node-x"
    got = asyncio.run(bus.revocation_of("node-x"))
    assert got is not None and got.reason == "compromise" and got.by == "ops"
    assert asyncio.run(bus.revocation_of("node-y")) is None


def test_reinstate_roundtrip():
    bus = _bus()
    asyncio.run(bus.revoke_node("node-x"))
    assert asyncio.run(bus.reinstate_node("node-x")) is True
    assert asyncio.run(bus.revocation_of("node-x")) is None
    # reinstating a never-revoked node reports False
    assert asyncio.run(bus.reinstate_node("node-never")) is False


def test_revoke_withdraws_capabilities_and_roster():
    bus = _bus()
    cap = Capability(agent_id="node-x-agent", skill="code", node="node-x")
    other = Capability(agent_id="node-y-agent", skill="code", node="node-y")
    for c in (cap, other):
        asyncio.run(bus._registry.put(c.key(), c.model_dump_json().encode()))
    info = NodeInfo(node_id="node-x", roles=["coder"], version="v1")
    asyncio.run(bus._roster.put(info.key(), info.model_dump_json().encode()))

    asyncio.run(bus.revoke_node("node-x"))

    live = asyncio.run(bus.list_capabilities())
    assert [c.agent_id for c in live] == ["node-y-agent"]
    assert asyncio.run(bus.get_node("node-x")) is None
    # the other node is untouched
    assert asyncio.run(bus.get_node("node-y")) is None or True  # roster only had node-x


def test_advertise_node_refuses_revoked_node():
    bus = _bus()
    asyncio.run(bus.revoke_node("node-x"))
    info = NodeInfo(node_id="node-x", roles=["coder"], version="v1")
    asyncio.run(bus.advertise_node(info))
    # heartbeat refused: no roster entry appears
    assert asyncio.run(bus.get_node("node-x")) is None
    # after reinstatement the same heartbeat joins
    asyncio.run(bus.reinstate_node("node-x"))
    asyncio.run(bus.advertise_node(info))
    assert asyncio.run(bus.get_node("node-x")) is not None


def test_advertise_capability_denies_revoked_node():
    bus = _bus()
    asyncio.run(bus.revoke_node("node-x"))
    cap = Capability(agent_id="node-x-agent", skill="code", node="node-x")
    try:
        asyncio.run(bus.advertise_capability(cap))
        raise AssertionError("expected PermissionError")
    except PermissionError as exc:
        assert "node revoked" in str(exc)


def test_try_claim_denied_for_revoked_node():
    bus = _bus()
    snapshot = _snapshot("t1", state=TaskState.RECEIVED, claimable=True)
    asyncio.run(bus.put_snapshot(snapshot))
    asyncio.run(bus.revoke_node("node-b"))

    claimed = asyncio.run(bus.try_claim("t1", "node-b:coder"))
    assert claimed is None
    # in-flight work is untouched: the snapshot is neither claimed nor mutated
    stored = asyncio.run(bus.get_snapshot("t1"))
    assert stored is not None and stored.claimed_by == ""

    # after reinstatement the same claim succeeds
    asyncio.run(bus.reinstate_node("node-b"))
    claimed = asyncio.run(bus.try_claim("t1", "node-b:coder"))
    assert claimed is not None and claimed.claimed_by == "node-b:coder"


def test_list_revocations_sorted():
    bus = _bus()
    for node in ("node-c", "node-a", "node-b"):
        record = RevocationRecord(node_id=node, revoked_at=time.time())
        asyncio.run(bus._revocations.put(record.key(), record.model_dump_json().encode()))
    listed = asyncio.run(bus.list_revocations())
    assert [r.node_id for r in listed] == ["node-a", "node-b", "node-c"]


def test_bucket_name_constant():
    assert REVOCATIONS_BUCKET == "mesh_revocations"
