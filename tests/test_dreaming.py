"""Tests for the dreaming consolidation core (14.13 Step 6, design-first)."""

from memnc.dreaming import (
    DREAM_TOPIC,
    MIN_CLUSTER_SIZE,
    RANKING_TOPIC,
    cluster_observations,
    dream_bullets,
    pattern_bullet,
    ranking_bullet,
    supersession_bullets,
)
from memnc.observations import TaskObservation


def _obs(
    task_id: str,
    task: str,
    outcome: str = "done",
    route: dict | None = None,
    revisions: int = 0,
    error: str | None = None,
    observed_at: str = "2026-11-18T00:00:00+00:00",
) -> TaskObservation:
    return TaskObservation(
        task_id=task_id,
        trace_id=f"tr-{task_id}",
        task=task,
        outcome=outcome,
        route=route or {},
        revisions=revisions,
        error=error,
        observed_at=observed_at,
    )


RATE_LIMITER_TASK = (
    "Implement a rate limiter allowing bursts: N requests per window, "
    "burst tokens refill over time."
)
WORD_LADDER_TASK = (
    "Word ladder: transform START to END changing one letter per step, "
    "shortest path, lexicographic tie-break."
)
UNRELATED_TASK = (
    "Parse a CSV with quoted fields and emit a normalized JSON summary."
)


def test_singletons_form_their_own_clusters():
    obs = [
        _obs("t1", RATE_LIMITER_TASK),
        _obs("t2", WORD_LADDER_TASK),
        _obs("t3", UNRELATED_TASK),
    ]
    clusters = cluster_observations(obs)
    assert len(clusters) == 3
    assert all(len(c) == 1 for c in clusters)


def test_similar_tasks_cluster_together():
    obs = [
        _obs("t1", RATE_LIMITER_TASK),
        _obs("t2", RATE_LIMITER_TASK + " Second part: track per-client quotas."),
    ]
    clusters = cluster_observations(obs)
    assert len(clusters) == 1
    assert len(clusters[0]) == 2


def test_clustering_is_order_independent():
    a = _obs("t1", RATE_LIMITER_TASK)
    b = _obs("t2", RATE_LIMITER_TASK + " Extra detail here.")
    forward = cluster_observations([a, b])
    backward = cluster_observations([b, a])
    assert [[o.task_id for o in c] for c in forward] == [
        [o.task_id for o in c] for c in backward
    ]


def test_pattern_bullet_below_floor_is_none():
    assert pattern_bullet([_obs("t1", RATE_LIMITER_TASK)]) is None


def test_pattern_bullet_reports_rate_and_route():
    cluster = [
        _obs(
            "t1",
            RATE_LIMITER_TASK,
            route={"coder": "node-a:coder32b"},
            revisions=1,
        ),
        _obs(
            "t2",
            RATE_LIMITER_TASK + " quota tracking.",
            route={"coder": "node-a:coder32b"},
        ),
        _obs(
            "t3",
            RATE_LIMITER_TASK + " burst semantics.",
            outcome="failed",
            error="TimeoutError: planner call exceeded budget",
        ),
    ]
    text = pattern_bullet(cluster)
    assert text is not None
    assert "pattern " in text
    assert "2/3 runs done (67%)" in text
    assert "best route: coder=coder32b" in text
    assert "dominant failure: TimeoutError" in text


def test_pattern_bullet_deterministic_majority():
    """Ties resolve by value ascending — same cluster, same bullet."""
    cluster = [
        _obs("t1", RATE_LIMITER_TASK, route={"coder": "node-a:m1"}),
        _obs("t2", RATE_LIMITER_TASK + " x", route={"coder": "node-b:m2"}),
    ]
    one = pattern_bullet(cluster)
    two = pattern_bullet(list(reversed(cluster)))
    assert one == two


def test_supersession_fires_on_flipped_route():
    early = _obs(
        "t1",
        RATE_LIMITER_TASK,
        route={"coder": "node-a:m1"},
        observed_at="2026-11-16T00:00:00+00:00",
    )
    late = _obs(
        "t2",
        RATE_LIMITER_TASK + " x",
        route={"coder": "node-b:m2"},
        observed_at="2026-11-18T00:00:00+00:00",
    )
    bullets = supersession_bullets([early, late])
    assert len(bullets) == 1
    assert "preferred route now coder=m2" in bullets[0]
    assert "supersedes coder=m1" in bullets[0]


def test_supersession_silent_when_route_stable():
    early = _obs(
        "t1",
        RATE_LIMITER_TASK,
        route={"coder": "node-a:m1"},
        observed_at="2026-11-16T00:00:00+00:00",
    )
    late = _obs(
        "t2",
        RATE_LIMITER_TASK + " x",
        route={"coder": "node-a:m1"},
        observed_at="2026-11-18T00:00:00+00:00",
    )
    assert supersession_bullets([early, late]) == []


def test_supersession_silent_without_a_success_on_one_side():
    early = _obs(
        "t1",
        RATE_LIMITER_TASK,
        outcome="failed",
        error="boom",
        observed_at="2026-11-16T00:00:00+00:00",
    )
    late = _obs(
        "t2",
        RATE_LIMITER_TASK + " x",
        route={"coder": "node-b:m2"},
        observed_at="2026-11-18T00:00:00+00:00",
    )
    assert supersession_bullets([early, late]) == []


def test_ranking_bullet_needs_qualified_pairs():
    thin = [
        _obs("t1", RATE_LIMITER_TASK, route={"planner": "n:p1", "coder": "n:c1"}),
    ]
    assert ranking_bullet(thin) is None
    qualified = thin + [
        _obs(
            "t2",
            RATE_LIMITER_TASK + " x",
            route={"planner": "n:p1", "coder": "n:c1"},
        ),
        _obs("t3", WORD_LADDER_TASK, route={"planner": "n:p2", "coder": "n:c2"}),
    ]
    text = ranking_bullet(qualified)
    assert text is not None
    assert text.startswith("top team pairs: c1|+|p1 2/2")


def test_dream_pass_empty_on_singleton_mesh():
    obs = [_obs("t1", RATE_LIMITER_TASK), _obs("t2", WORD_LADDER_TASK)]
    assert dream_bullets(obs) == {}


def test_dream_pass_emits_all_three_surfaces():
    obs = [
        _obs("t1", RATE_LIMITER_TASK, route={"coder": "n:m1"}),
        _obs("t2", RATE_LIMITER_TASK + " x", route={"coder": "n:m1"}),
        _obs("t3", WORD_LADDER_TASK, route={"planner": "n:p", "coder": "n:c"}),
        _obs("t4", WORD_LADDER_TASK + " y", route={"planner": "n:p", "coder": "n:c"}),
    ]
    out = dream_bullets(obs)
    assert DREAM_TOPIC in out
    patterns = [t for t in out[DREAM_TOPIC] if t.startswith("pattern ")]
    assert len(patterns) == 2  # one per cluster
    assert any("top team pairs" in t for t in out[RANKING_TOPIC])
    # no supersession on stable routes
    assert not any("supersedes" in t for t in out[DREAM_TOPIC])


def test_floor_constant_is_two():
    assert MIN_CLUSTER_SIZE == 2
