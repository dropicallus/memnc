"""T7.5: model demand planning — pure-logic tests (planner + request lifecycle)."""

from __future__ import annotations

from typing import Any

from memnc.schema import ModelRequest, NodeInfo, TaskNeeds, plan_model_request

_DEFAULTS: dict[str, Any] = dict(
    node_id="n1", roles=["coder"], models=["gemma-4-31b"],
    vram_gb=20.0, gpu="rx 6800 xt",
)


def _node(**kw: Any) -> NodeInfo:
    return NodeInfo(**{**_DEFAULTS, **kw})


def test_no_model_constraint_never_requests():
    assert plan_model_request(None, [_node()], "t1") is None
    assert plan_model_request(TaskNeeds(vram_gb=8), [_node()], "t1") is None


def test_model_already_loaded_is_served_by_claim_market():
    nodes = [_node(models=["gemma-4-31b"])]
    assert plan_model_request(TaskNeeds(model="gemma-4-31b"), nodes, "t1") is None


def test_model_on_disk_proposes_load():
    nodes = [_node(disk_models=["apex-35b"])]
    request = plan_model_request(TaskNeeds(model="apex-35b"), nodes, "t1")
    assert request is not None
    assert request.action == "load"
    assert request.nodes_disk == ["n1"]
    assert request.status == "pending"
    assert request.task_id == "t1"


def test_model_nowhere_proposes_pull():
    nodes = [_node(disk_models=["apex-35b"])]
    request = plan_model_request(TaskNeeds(model="mistral-7b"), nodes, "t1")
    assert request is not None
    assert request.action == "pull"
    assert request.nodes_disk == []


def test_unique_request_ids():
    nodes = [_node(disk_models=["apex-35b"])]
    a = plan_model_request(TaskNeeds(model="apex-35b"), nodes, "t1")
    b = plan_model_request(TaskNeeds(model="apex-35b"), nodes, "t2")
    assert a is not None and b is not None
    assert a.request_id != b.request_id


def test_request_key_roundtrip():
    request = ModelRequest(request_id="mreq_abc", model="apex-35b")
    assert "mreq_abc" in request.key()
