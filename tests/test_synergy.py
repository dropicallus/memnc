from memnc.observations import TaskObservation
from memnc.synergy import (
    compute_server_success,
    compute_synergy_metrics,
    format_synergy,
    should_withdraw,
)


def _obs(task_id, outcome, route, revisions=0):
    return TaskObservation(
        task_id=task_id,
        trace_id=task_id,
        task="t",
        outcome=outcome,
        route=route,
        revisions=revisions,
    )


ROUTE_A = {"coder": "n1:model-a", "critic": "n2:model-b"}


def test_empty_observations() -> None:
    assert compute_synergy_metrics([]) == {}


def test_single_pair_counts() -> None:
    obs = [_obs("t1", "done", ROUTE_A), _obs("t2", "failed", ROUTE_A)]
    m = compute_synergy_metrics(obs)
    assert list(m) == ["model-a|+|model-b"]
    e = m["model-a|+|model-b"]
    assert e["total"] == 2
    assert e["successes"] == 1
    assert abs(e["success_ratio"] - 0.5) < 1e-9


def test_mean_revisions() -> None:
    obs = [
        _obs("t1", "done", ROUTE_A, revisions=1),
        _obs("t2", "done", ROUTE_A, revisions=3),
    ]
    e = compute_synergy_metrics(obs)["model-a|+|model-b"]
    assert abs(e["mean_revisions"] - 2.0) < 1e-9


def test_same_model_two_roles_no_pair() -> None:
    route = {"coder": "n1:model-a", "critic": "n2:model-a"}
    assert compute_synergy_metrics([_obs("t1", "done", route)]) == {}


def test_pairs_sorted_by_ratio_then_name() -> None:
    r_ab = {"coder": "n1:model-a", "critic": "n2:model-b"}
    r_ac = {"coder": "n1:model-a", "critic": "n2:model-c"}
    obs = [
        _obs("t1", "failed", r_ab),
        _obs("t2", "done", r_ac),
        _obs("t3", "done", r_ac),
    ]
    m = compute_synergy_metrics(obs)
    assert list(m) == ["model-a|+|model-c", "model-a|+|model-b"]


def test_failed_tasks_count_in_total() -> None:
    obs = [_obs("t1", "failed", ROUTE_A), _obs("t2", "cancelled", ROUTE_A)]
    e = compute_synergy_metrics(obs)["model-a|+|model-b"]
    assert e["total"] == 2
    assert e["successes"] == 0


def test_format_empty() -> None:
    assert format_synergy({}) == "no model pairs observed yet"


def test_format_table() -> None:
    m = compute_synergy_metrics([_obs("t1", "done", ROUTE_A, revisions=2)])
    lines = format_synergy(m).splitlines()
    assert lines[0].startswith("pair |")
    assert "model-a + model-b" in lines[1]
    assert "| 1 | 1 | 1.00 | 2.00" in lines[1]


# -- 14.13 step 5: per-server track record + claim gate ---------------------

ROUTE_MIXED = {
    "planner": "node-a-planner:qwen2.5:7b",
    "coder": "node-b-coder:llama3.2:3b",
}


def test_server_success_empty() -> None:
    assert compute_server_success([]) == {}


def test_server_success_counts_per_server() -> None:
    obs = [
        _obs("t1", "done", ROUTE_MIXED),
        _obs("t2", "done", ROUTE_MIXED),
        _obs("t3", "failed", ROUTE_MIXED),
        _obs("t4", "done", {"planner": "node-a-planner:llama3.2:3b"}),
    ]
    stats = compute_server_success(obs)
    assert stats["node-a-planner:qwen2.5:7b"] == {
        "total": 3, "successes": 2, "success_ratio": 2 / 3,
    }
    assert stats["node-b-coder:llama3.2:3b"]["total"] == 3
    assert stats["node-a-planner:llama3.2:3b"]["success_ratio"] == 1.0


def test_withdraw_unknown_server_keeps_claiming() -> None:
    assert not should_withdraw({}, "node-x:model")


def test_withdraw_insufficient_samples_keeps_claiming() -> None:
    stats = {"node-x:model": {"total": 2, "successes": 0, "success_ratio": 0.0}}
    assert not should_withdraw(stats, "node-x:model", min_samples=3)


def test_withdraw_bad_record() -> None:
    stats = {"node-x:model": {"total": 3, "successes": 0, "success_ratio": 0.0}}
    assert should_withdraw(stats, "node-x:model")


def test_withdraw_decent_record_keeps_claiming() -> None:
    stats = {"node-x:model": {"total": 3, "successes": 2, "success_ratio": 2 / 3}}
    assert not should_withdraw(stats, "node-x:model")


def test_withdraw_exactly_at_ratio_threshold_keeps_claiming() -> None:
    # ratio == min_ratio is NOT below it -> the server keeps claiming
    stats = {"node-x:model": {"total": 3, "successes": 1, "success_ratio": 1 / 3}}
    assert not should_withdraw(stats, "node-x:model", min_ratio=1 / 3)


def test_one_in_three_keeps_claiming_at_default() -> None:
    # default 0.3: a single success in 3 attempts is NOT below threshold —
    # only clearly-bad records (0/3) bench themselves
    stats = {"node-x:model": {"total": 3, "successes": 1, "success_ratio": 1 / 3}}
    assert not should_withdraw(stats, "node-x:model")
