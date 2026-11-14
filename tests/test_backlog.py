"""Tests for the KV-backed backlog with CAS claiming."""

import asyncio
from datetime import UTC, datetime, timedelta

from nats.js.errors import NotFoundError

from memnc.backlog import (
    BACKLOG_BUCKET,
    CLAIM_STALE_S,
    MAX_ATTEMPTS,
    BacklogStore,
    BacklogTask,
    decompose_plan,
)


class FakeEntry:
    def __init__(self, value: bytes, revision: int) -> None:
        self.value = value
        self.revision = revision


class FakeKV:
    """Minimal KV: get/put/update with revision counting (CAS semantics)."""

    def __init__(self) -> None:
        self.data: dict[str, tuple[bytes, int]] = {}
        self._rev = 0

    async def get(self, key: str) -> FakeEntry:
        if key not in self.data:
            raise NotFoundError()
        value, rev = self.data[key]
        return FakeEntry(value, rev)

    async def put(self, key: str, value: bytes) -> int:
        self._rev += 1
        self.data[key] = (value, self._rev)
        return self._rev

    async def update(self, key: str, value: bytes, last: int) -> int:
        if key not in self.data or self.data[key][1] != last:
            raise RuntimeError("wrong last sequence number")
        return await self.put(key, value)

    async def keys(self) -> list[str]:
        return sorted(self.data)


class FakeJS:
    def __init__(self) -> None:
        self.kv = FakeKV()
        self.created: list[str] = []

    async def key_value(self, bucket: str):
        if bucket != BACKLOG_BUCKET:
            raise RuntimeError("no such bucket")
        return self.kv

    async def create_key_value(self, bucket: str = "", **_kwargs):
        self.created.append(bucket)
        return self.kv


def _store() -> tuple[BacklogStore, FakeJS]:
    js = FakeJS()
    return BacklogStore(js), js  # type: ignore[arg-type]


def test_add_and_list_roundtrip():
    store, _ = _store()
    task = asyncio.run(store.add("do the thing", prompt="p", plan_id="plan1"))
    tasks = asyncio.run(store.list())
    assert [t.id for t in tasks] == [task.id]
    assert tasks[0].title == "do the thing"
    assert tasks[0].status == "planned"


def test_claim_is_exclusive():
    store, _ = _store()
    task = asyncio.run(store.add("build it"))
    first = asyncio.run(store.claim(task.id, "driver-a"))
    second = asyncio.run(store.claim(task.id, "driver-b"))
    assert first is not None and first.claimant == "driver-a"
    assert second is None


def test_claim_nonexistent_returns_none():
    store, _ = _store()
    assert asyncio.run(store.claim("b-missing", "driver-a")) is None


def test_claim_only_planned():
    store, _ = _store()
    task = asyncio.run(store.add("build it"))
    asyncio.run(store.mark(task.id, "done"))
    assert asyncio.run(store.claim(task.id, "driver-a")) is None


def test_mark_done_and_failed_with_retry_budget():
    store, _ = _store()
    task = asyncio.run(store.add("flaky job"))
    asyncio.run(store.claim(task.id, "driver-a"))
    for expected_attempts in range(1, MAX_ATTEMPTS):
        t = asyncio.run(store.mark(task.id, "failed", mesh_task_id="m1"))
        assert t is not None
        assert t.attempts == expected_attempts
        assert t.status == "planned"  # back to planned for retry
    t = asyncio.run(store.mark(task.id, "failed"))
    assert t is not None
    assert t.status == "failed"  # budget exhausted: stays failed
    assert t.attempts == MAX_ATTEMPTS


def test_next_claimable_respects_deps_and_skips_locked():
    store, _ = _store()
    a = asyncio.run(store.add("first"))
    b = asyncio.run(store.add("second", deps=[a.id]))
    # b depends on a: not claimable yet
    assert asyncio.run(store.next_claimable("driver-a")) is not None
    # driver-b claimed it first -> a is claimed, next claimable is None
    got = asyncio.run(store.next_claimable("driver-b"))
    assert got is None  # a was claimed by driver-a inside the previous call
    asyncio.run(store.mark(a.id, "done"))
    got = asyncio.run(store.next_claimable("driver-b"))
    assert got is not None and got.id == b.id


def test_next_claimable_none_when_all_terminal():
    store, _ = _store()
    asyncio.run(store.add("only one"))
    t = asyncio.run(store.next_claimable("d"))
    assert t is not None
    asyncio.run(store.mark(t.id, "done"))
    assert asyncio.run(store.next_claimable("d2")) is None


def test_stale_claim_is_reclaimed():
    store, _ = _store()
    task = asyncio.run(store.add("abandoned"))
    asyncio.run(store.claim(task.id, "dead-driver"))
    # backdate the claim past CLAIM_STALE_S
    stale = datetime.now(UTC) - timedelta(seconds=CLAIM_STALE_S + 60)
    async def _backdate():
        kv = await store._ensure_kv()
        entry = await kv.get(task.id)
        obj = BacklogTask.model_validate_json(entry.value)
        obj.claimed_at = stale.strftime("%Y-%m-%dT%H:%M:%SZ")
        await kv.put(task.id, obj.model_dump_json().encode())

    asyncio.run(_backdate())
    reclaimed = asyncio.run(store.claim(task.id, "driver-b"))
    assert reclaimed is not None and reclaimed.claimant == "driver-b"
    # fresh claim is NOT stealable
    fresh = asyncio.run(store.claim(task.id, "driver-c"))
    assert fresh is None


def test_decompose_plan_numbered_lines():
    plan = "1. write the parser\n2. add tests for the parser\n3. update docs"
    assert decompose_plan(plan) == [
        "write the parser",
        "add tests for the parser",
        "update docs",
    ]


def test_decompose_plan_bullets():
    plan = "- first thing\n* second thing"
    assert decompose_plan(plan) == ["first thing", "second thing"]


def test_decompose_plan_paragraphs():
    plan = "Build the thing.\n\nThen test the thing.\n\n\nShip it."
    assert decompose_plan(plan) == ["Build the thing.", "Then test the thing.", "Ship it."]


def test_decompose_plan_single_block_is_one_task():
    assert decompose_plan("just do the one thing") == ["just do the one thing"]


def test_decompose_plan_empty():
    assert decompose_plan("   \n  ") == []


# -- weak->strong routing helpers -------------------------------------------

def _failed_task(attempts: int = 3, strong_retry: bool = False) -> BacklogTask:
    return BacklogTask(
        title="write the report",
        prompt="write the report",
        status="failed",
        attempts=attempts,
        strong_retry=strong_retry,
    )


def test_should_strong_retry_true_when_terminal_and_configured() -> None:
    from memnc.backlog import should_strong_retry
    assert should_strong_retry(_failed_task(attempts=3), "node-a") is True


def test_should_strong_retry_false_without_strong_node() -> None:
    from memnc.backlog import should_strong_retry
    assert should_strong_retry(_failed_task(), "") is False


def test_should_strong_retry_false_while_attempts_remain() -> None:
    # mark() flips status back to planned while the budget lasts; a task
    # still mid-budget must not spawn a strong retry.
    from memnc.backlog import MAX_ATTEMPTS, should_strong_retry
    t = _failed_task(attempts=MAX_ATTEMPTS - 1)
    t.status = "planned"  # mark() behaviour: retried in-pool first
    assert should_strong_retry(t, "node-a") is False


def test_should_strong_retry_never_loops() -> None:
    from memnc.backlog import should_strong_retry
    t = _failed_task(strong_retry=True)
    assert should_strong_retry(t, "node-a") is False


def test_make_strong_retry_copies_prompt_and_sets_target() -> None:
    from memnc.backlog import make_strong_retry
    src = _failed_task()
    src.plan_id = "night4"
    src.deps = ["b-abc"]
    retry = make_strong_retry(src, "node-a")
    assert retry.prompt == src.prompt
    assert retry.plan_id == "night4"
    assert retry.deps == ["b-abc"]
    assert retry.strong_retry is True
    assert retry.prefer_node == "node-a"
    assert retry.title.startswith("[strong-retry] ")
    assert retry.id != src.id
