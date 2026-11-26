"""T5: planner allocation — first claim wins, loaded model beats free VRAM."""

from __future__ import annotations

from memnc.orchestrator import Allocator
from memnc.schema import GpuHealth, ModelEntry, NodeHealth


def _health(node: str, free_mb: float, loaded: dict[str, ModelEntry]) -> NodeHealth:
    return NodeHealth(
        node=node,
        gpu=GpuHealth(vram_free_mb=free_mb, vram_total_mb=16384.0),
        models=loaded,
    )


def _entry(vram_mb: float) -> ModelEntry:
    return ModelEntry(loaded=True, vram_mb=vram_mb)


def test_loaded_model_wins_over_more_free_vram():
    alloc = Allocator(twin_stale_s=30)
    alloc.update_health(
        _health("node-a", 8000.0, {}),
        _health("node-b", 2000.0, {"coder-7b": _entry(1800.0)}),
    )
    result = alloc.select("coder-7b", "t1")
    assert result.ok is True
    assert result.node == "node-b"  # load time 0 beats raw VRAM


def test_size_only_fits_one_node():
    alloc = Allocator(twin_stale_s=30)
    alloc.update_health(
        _health("node-a", 4000.0, {}),
        _health("node-b", 8000.0, {}),
    )
    result = alloc.select("big-13b", "t2", size_mb=5000.0)
    assert result.ok is True
    assert result.node == "node-b"


def test_model_fits_nowhere():
    alloc = Allocator(twin_stale_s=30)
    alloc.update_health(_health("node-a", 4000.0, {}))
    result = alloc.select("huge-70b", "t3", size_mb=9000.0)
    assert result.ok is False
    assert result.reason == "no_node"
    assert result.node is None


def test_stale_node_is_excluded():
    import time

    alloc = Allocator(twin_stale_s=30)
    health = _health("node-a", 8000.0, {})
    health.ts = "2026-01-01T00:00:00Z"  # ancient
    alloc.update_health(health)
    time.sleep(0.01)
    result = alloc.select("any", "t4", size_mb=100.0)
    assert result.ok is False
    assert result.reason == "no_node"


def test_double_claim_second_reallocates_to_free_node():
    alloc = Allocator(twin_stale_s=30)
    alloc.update_health(
        _health("node-a", 8000.0, {}),
        _health("node-b", 8000.0, {}),
    )
    first = alloc.select("m", "t5", size_mb=7000.0)
    second = alloc.select("m", "t5", size_mb=7000.0)
    assert first.ok and second.ok
    # same task → same claim (idempotent), no double booking
    assert second.node == first.node


def test_claim_reservation_excludes_node_for_other_task():
    alloc = Allocator(twin_stale_s=30)
    alloc.update_health(
        _health("node-a", 8000.0, {}),
        _health("node-b", 8000.0, {}),
    )
    first = alloc.select("m", "t6", size_mb=7000.0)
    other = alloc.select("m2", "t7", size_mb=7000.0)
    assert other.node != first.node  # node capacity consumed by first claim


def test_no_health_at_all():
    alloc = Allocator(twin_stale_s=30)
    result = alloc.select("m", "t8")
    assert result.ok is False
    assert result.reason == "no_node"


def test_release_frees_the_reservation_for_the_next_claim():
    from memnc.orchestrator import Allocator
    from memnc.schema import AllocResult

    alloc = Allocator()
    alloc._claim(AllocResult(ok=True, model="m", node="n1"), "t1", 4000.0)
    assert alloc._reserved_mb["n1"] == 4000.0
    alloc.release("t1")
    assert "n1" not in alloc._reserved_mb
    assert "t1" not in alloc._claims


def test_release_is_idempotent():
    from memnc.orchestrator import Allocator
    from memnc.schema import AllocResult

    alloc = Allocator()
    alloc._claim(AllocResult(ok=True, model="m", node="n1"), "t1", 1000.0)
    alloc.release("t1")
    alloc.release("t1")
    assert alloc._reserved_mb == {}
