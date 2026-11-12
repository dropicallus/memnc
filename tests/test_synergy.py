from memnc.observations import TaskObservation
from memnc.synergy import compute_synergy_metrics, format_synergy


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
