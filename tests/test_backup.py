"""Cold-standby backup/restore (docs/operations.md Duo topology)."""

from unittest.mock import AsyncMock

from test_trust_fake import FakeKV

from memnc.backup import (
    DURABLE_BUCKETS,
    EPHEMERAL_BUCKETS,
    backup_bucket_names,
    dump_buckets,
    restore_buckets,
)
from memnc.config import Settings


def _fake_js(buckets: dict[str, FakeKV]):
    js = type("JS", (), {})()
    js.key_value = AsyncMock(side_effect=lambda name: buckets[name])
    return js


def _bus(buckets: dict[str, FakeKV]):
    bus = type("Bus", (), {})()
    bus.js = _fake_js(buckets)
    bus.settings = Settings()
    return bus


class FakeKVRestore(FakeKV):
    """FakeKV plus put/delete counters for restore assertions."""

    def __init__(self):
        super().__init__()
        object.__setattr__(self, "put_calls", [])
        object.__setattr__(self, "delete_calls", [])

    async def put(self, key, value):
        self.put_calls.append((key, value))
        return await super().put(key, value)

    async def delete(self, key):
        self.delete_calls.append(key)
        await super().delete(key)


class TestBackupBucketNames:
    def test_covers_durable_plus_main_state(self) -> None:
        names = backup_bucket_names(Settings())
        assert set(DURABLE_BUCKETS) <= set(names)
        assert names[-1] == "workflow_state"

    def test_ephemeral_buckets_never_backed_up(self) -> None:
        names = backup_bucket_names(Settings())
        assert not (set(EPHEMERAL_BUCKETS) & set(names))


class TestDump:
    async def test_dumps_all_entries_sorted(self) -> None:
        kv = FakeKV()
        await kv.put("task.b", b'{"id":"b"}')
        await kv.put("task.a", b'{"id":"a"}')
        bus = _bus({"workflow_state": kv})
        data = await dump_buckets(bus)
        assert list(data["buckets"]["workflow_state"]) == ["task.a", "task.b"]
        assert data["buckets"]["workflow_state"]["task.a"] == '{"id":"a"}'
        assert data["meta"]["kv_bucket"] == "workflow_state"
        assert "created_at" in data["meta"]

    async def test_absent_bucket_is_skipped(self) -> None:
        js = type("JS", (), {})()
        js.key_value = AsyncMock(side_effect=KeyError("mesh_trust"))
        bus = type("Bus", (), {})()
        bus.js = js
        bus.settings = Settings()
        kv = FakeKV()
        await kv.put("task.a", b"{}")
        # only main bucket exists on this deployment
        async def _kv(name):
            if name == "workflow_state":
                return kv
            raise KeyError(name)
        js.key_value = AsyncMock(side_effect=_kv)
        data = await dump_buckets(bus)
        assert set(data["buckets"]) == {"workflow_state"}

    async def test_empty_value_backed_up_as_empty_string(self) -> None:
        kv = FakeKV()
        kv._store["task.x"] = None
        bus = _bus({"workflow_state": kv})
        data = await dump_buckets(bus)
        assert data["buckets"]["workflow_state"]["task.x"] == ""


class TestRestore:
    async def test_restore_upserts_entries(self) -> None:
        kv = FakeKVRestore()
        await kv.put("task.live", b'{"id":"live"}')
        bus = _bus({"workflow_state": kv})
        data = {"buckets": {"workflow_state": {"task.a": '{"id":"a"}'}}}
        counts = await restore_buckets(bus, data)
        assert counts == {"workflow_state": 1}
        assert kv._store["task.a"] == b'{"id":"a"}'
        # upsert: live-only key survives without purge
        assert "task.live" in kv._store

    async def test_restore_purge_makes_exact_mirror(self) -> None:
        kv = FakeKVRestore()
        await kv.put("task.live", b'{"id":"live"}')
        bus = _bus({"workflow_state": kv})
        data = {"buckets": {"workflow_state": {"task.a": '{"id":"a"}'}}}
        await restore_buckets(bus, data, purge=True)
        assert "task.live" not in kv._store
        assert kv.delete_calls == ["task.live"]

    async def test_restore_missing_bucket_skipped(self) -> None:
        js = type("JS", (), {})()
        kv = FakeKVRestore()
        async def _kv(name):
            if name == "workflow_state":
                return kv
            raise KeyError(name)
        js.key_value = AsyncMock(side_effect=_kv)
        bus = type("Bus", (), {})()
        bus.js = js
        bus.settings = Settings()
        data = {
            "buckets": {
                "mesh_trust": {"agent-1": "{}"},
                "workflow_state": {"task.a": "{}"},
            }
        }
        counts = await restore_buckets(bus, data)
        assert counts == {"workflow_state": 1}

    async def test_roundtrip(self) -> None:
        src_kv = FakeKV()
        await src_kv.put("task.a", b'{"id":"a"}')
        await src_kv.put("rep.qwen", b'{"ok":true}')
        src = _bus({"workflow_state": src_kv})
        data = await dump_buckets(src)
        dst_kv = FakeKVRestore()
        dst = _bus({"workflow_state": dst_kv})
        await restore_buckets(dst, data, purge=True)
        again = await dump_buckets(dst)
        assert again["buckets"] == data["buckets"]
