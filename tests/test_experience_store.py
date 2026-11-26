"""Tests for the ACE-style experience store (roadmap 15.3 item 5)."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from memnc.experience_store import (
    MAX_PRUNE_RATIO,
    ExperienceStore,
    MergeReport,
    record_bench_run,
)


def test_append_ids_continue_per_topic() -> None:
    store = ExperienceStore()
    store.append("bench-arm-A", "hard tier: 6/8 success")
    store.append("bench-arm-A", "mean tokens/task 610", source="bench")
    store.append("bench-arm-B", "hard tier: 8/8 success")
    ids = [b.id for b in store.bullets]
    assert ids == ["bench-arm-A-1", "bench-arm-A-2", "bench-arm-B-1"]


def test_save_load_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "store.json"
        s1 = ExperienceStore(path)
        s1.append("t", "one detail sentence", source="bench")
        s1.save()
        s2 = ExperienceStore(path).load()
        assert [b.text for b in s2.bullets] == ["one detail sentence"]
        assert s2.bullets[0].source == "bench"


def test_merge_drops_exact_and_near_duplicates() -> None:
    store = ExperienceStore()
    existing = "Hard tier separates the two arms under load"
    store.append("t", existing)
    report = store.merge("t", [existing])
    assert report.added == []
    assert len(report.duplicates) == 1
    # Near-duplicate: >= 0.8 token overlap, one extra word -> longer text wins.
    report2 = store.merge("t", ["Hard tier separates the two arms under load consistently"])
    assert report2.added == ["Hard tier separates the two arms under load consistently"]
    texts = [b.text for b in store.topic("t")]
    assert texts == ["Hard tier separates the two arms under load consistently"]


def test_merge_short_duplicate_keeps_existing() -> None:
    store = ExperienceStore()
    long_text = "Hard tier tasks with a nasty edge case need the critic loop"
    store.append("t", long_text)
    report = store.merge("t", ["Hard tier tasks with a nasty edge case need critic"])
    assert report.added == []
    assert store.topic("t")[0].text == long_text


def test_merge_is_monotone_never_shrinks() -> None:
    store = ExperienceStore()
    for i in range(10):
        store.append("t", f"Distinct detail number {i} about arm behaviour")
    # shorter near-duplicates: existing (longer) bullet is kept
    report = store.merge("t", [f"Distinct detail number {i} about arm" for i in range(10)])
    assert report.rejected_reason == ""
    assert report.added == []  # all near-duplicates folded, existing kept
    assert report.duplicates  # folded, not added
    assert len(store.topic("t")) == 10  # untouched


def test_compress_rejects_over_compression_brevity_bias() -> None:
    store = ExperienceStore()
    for i in range(10):
        store.append("t", f"Distinct detail number {i} about arm behaviour")
    report = store.compress("t", ["compressed summary bullet"], source="llm")
    assert report.rejected_reason != ""
    assert len(store.topic("t")) == 10  # untouched


def test_compress_allows_small_reduction_and_renumbers() -> None:
    store = ExperienceStore()
    for i in range(10):
        store.append("t", f"Distinct detail number {i} about arm behaviour")
    new = [f"Compressed detail {i}" for i in range(8)]  # 20% reduction: allowed
    report = store.compress("t", new, source="llm")
    assert report.rejected_reason == ""
    assert [b.text for b in store.topic("t")] == new
    assert [b.id for b in store.topic("t")] == [f"t-{i}" for i in range(1, 9)]


def test_compress_on_empty_topic_is_allowed() -> None:
    store = ExperienceStore()
    report = store.compress("fresh", ["a bullet"])
    assert report.rejected_reason == ""
    assert len(store.topic("fresh")) == 1


def test_merge_empty_is_noop() -> None:
    store = ExperienceStore()
    assert store.merge("t", []) == MergeReport()


def test_record_bench_run_produces_arm_bullets() -> None:
    rows = [
        {"arm": "A", "task_id": "t1", "tier": "hard", "success": False,
         "tokens_in": 500, "tokens_out": 100, "elapsed_s": 30.0,
         "issues": ["non-monotonic ts mishandled"]},
        {"arm": "A", "task_id": "t2", "tier": "hard", "success": True,
         "tokens_in": 500, "tokens_out": 100, "elapsed_s": 30.0},
        {"arm": "B", "task_id": "t1", "tier": "hard", "success": True,
         "tokens_in": 2000, "tokens_out": 300, "elapsed_s": 90.0},
    ]
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "run.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
        store = ExperienceStore()
        reports = record_bench_run(path, store)
    assert sorted(reports) == ["bench-arm-A", "bench-arm-B"]
    a_texts = [b.text for b in store.topic("bench-arm-A")]
    assert any("hard tier: 1/2 success" in t for t in a_texts)
    assert any("Failed task t1 (hard): non-monotonic ts mishandled" in t for t in a_texts)
    assert any("mean tokens/task" in t for t in a_texts)


def test_record_bench_run_is_merge_deterministic() -> None:
    rows = [{"arm": "A", "task_id": "t1", "tier": "hard", "success": True,
             "tokens_in": 1, "tokens_out": 1, "elapsed_s": 1.0}]
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "run.jsonl"
        path.write_text(json.dumps(rows[0]), encoding="utf-8")
        s1 = ExperienceStore()
        record_bench_run(path, s1)
        before = len(s1.topic("bench-arm-A"))
        reports = record_bench_run(path, s1)  # same run again: all duplicates
    assert len(s1.topic("bench-arm-A")) == before  # no growth
    assert all(r.duplicates and not r.added for r in reports.values())


def test_ratio_constant_matches_design() -> None:
    assert MAX_PRUNE_RATIO == 0.3


def test_exclusive_creates_a_lock_file_beside_the_store(tmp_path):
    from memnc.experience_store import exclusive

    target = tmp_path / "store.json"
    with exclusive(target):
        assert (tmp_path / "store.json.lock").exists()
