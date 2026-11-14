"""Tests for 14.13 step 4: similarity + ranked attention (experience.py)."""

from datetime import UTC, datetime, timedelta

from memnc.experience import (
    format_ranked,
    jaccard,
    ranked_attention,
    tokenize,
)
from memnc.observations import TaskObservation

_NOW = datetime(2026, 11, 14, 12, 0, 0, tzinfo=UTC)


def _obs(task_id: str, task: str, outcome: str = "done", hours_ago: float = 0.0):
    ts = _NOW - timedelta(hours=hours_ago)
    return TaskObservation(
        task_id=task_id,
        trace_id=f"tr-{task_id}",
        task=task,
        outcome=outcome,
        observed_at=ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


class TestTokenize:
    def test_lowercases_and_stops(self):
        assert tokenize("Parse the CSV File") == {"parse", "csv", "file"}

    def test_drops_short_and_stopwords(self):
        assert tokenize("an on it fix bug") == {"fix", "bug"}

    def test_empty(self):
        assert tokenize("!! 42") == set()


class TestJaccard:
    def test_identical(self):
        s = {"a", "b"}
        assert jaccard(s, s) == 1.0

    def test_disjoint(self):
        assert jaccard({"a"}, {"b"}) == 0.0

    def test_empty_side(self):
        assert jaccard(set(), {"a"}) == 0.0

    def test_partial_overlap(self):
        assert abs(jaccard({"a", "b"}, {"b", "c"}) - 1 / 3) < 1e-9


class TestRankedAttention:
    def test_ranks_more_similar_first(self):
        obs = [
            _obs("t1", "write a kafka consumer for telemetry events"),
            _obs("t2", "write a kafka consumer for audit events"),
            _obs("t3", "fix the css layout on the login page"),
        ]
        out = ranked_attention("write a kafka consumer for audit events", obs, now=_NOW)
        assert [r["observation"].task_id for r in out][:2] == ["t2", "t1"]
        assert out[0]["score"] > out[1]["score"]

    def test_drops_below_min_similarity(self):
        obs = [_obs("t1", "completely unrelated thing here")]
        assert ranked_attention("kafka consumer events", obs, now=_NOW) == []

    def test_success_beats_failure(self):
        ok = _obs("t-ok", "implement retry queue worker", "done")
        bad = _obs("t-bad", "implement retry queue worker", "failed")
        out = ranked_attention("implement retry queue worker", [bad, ok], now=_NOW)
        assert out[0]["observation"].task_id == "t-ok"
        assert abs(out[0]["score"] - 1.2) < 1e-9  # 1.0 * corroboration(1 ally)
        assert abs(out[1]["score"] - 0.3) < 1e-9  # 0.25 * corroboration(1 ally)

    def test_half_life_decay(self):
        task = "parse csv import tool"
        fresh = _obs("t-fresh", task, hours_ago=0.0)
        old = _obs("t-old", task, hours_ago=14 * 24)  # one half-life
        out = ranked_attention(task, [old, fresh], now=_NOW)
        by_id = {r["observation"].task_id: r for r in out}
        assert abs(by_id["t-old"]["score"] - 0.5 * by_id["t-fresh"]["score"]) < 1e-9
        assert abs(by_id["t-old"]["age_days"] - 14.0) < 0.01

    def test_corroboration_boosts_clumps(self):
        task = "export telemetry metrics to json"
        solo = [_obs("t1", task)]
        clump = [
            _obs("t1", task),
            _obs("t2", "export telemetry metrics to json file"),
        ]
        solo_out = ranked_attention(task, solo, now=_NOW)
        clump_out = ranked_attention(task, clump, now=_NOW)
        assert clump_out[0]["score"] > solo_out[0]["score"]
        # capped at 2.0 even with many allies
        many = clump + [_obs(f"t{i}", task) for i in range(3, 20)]
        capped = ranked_attention(task, many, now=_NOW)
        base = solo_out[0]["score"]
        assert capped[0]["score"] <= base * 2.0 + 1e-9

    def test_deterministic_ordering(self):
        obs = [
            _obs("t1", "kafka consumer events", hours_ago=1),
            _obs("t2", "kafka consumer events", hours_ago=2),
        ]
        a = ranked_attention("kafka consumer events", obs, now=_NOW)
        b = ranked_attention("kafka consumer events", obs, now=_NOW)
        assert [r["observation"].task_id for r in a] == [
            r["observation"].task_id for r in b
        ]

    def test_cancelled_carries_no_reward(self):
        obs = [_obs("t1", "build the widget exporter", "cancelled")]
        assert ranked_attention("build the widget exporter", obs, now=_NOW) == []


class TestFormatRanked:
    def test_empty(self):
        assert format_ranked([]) == "no similar observations"

    def test_rows(self):
        obs = _obs("t1", "parse csv import tool with validation")
        out = format_ranked([{"observation": obs, "similarity": 1.0, "age_days": 2.0, "score": 0.9}])
        lines = out.splitlines()
        assert lines[0].startswith("score | sim | age(d)")
        assert "t1" in lines[1] and "done" in lines[1]

    def test_limit(self):
        obs = [_obs(f"t{i}", "parse csv import tool") for i in range(5)]
        results = ranked_attention("parse csv import tool", obs, now=_NOW)
        assert len(format_ranked(results, limit=2).splitlines()) == 3
