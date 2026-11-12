"""T6: node roster — pure-logic tests (satisfies predicate + entry merge)."""

from __future__ import annotations

from typing import Any

from memnc.schema import NodeInfo, TaskNeeds, satisfies

_DEFAULTS: dict[str, Any] = dict(
    node_id="n1", roles=["coder", "critic"], models=["gemma-4-31b"],
    vram_gb=20.0, gpu="test-gpu-a",
)


def _node(**kw: Any) -> NodeInfo:
    return NodeInfo(**{**_DEFAULTS, **kw})


def test_no_needs_is_unconstrained():
    assert satisfies(None, None, "coder")
    assert satisfies(None, _node(), "critic")


def test_needs_without_roster_entry_is_fail_closed():
    assert not satisfies(TaskNeeds(vram_gb=1), None, "coder")


def test_vram_requirement():
    needs = TaskNeeds(vram_gb=20)
    assert satisfies(needs, _node(), "coder")
    assert not satisfies(needs, _node(vram_gb=19.9), "coder")


def test_model_requirement():
    needs = TaskNeeds(model="gemma-4-31b")
    assert satisfies(needs, _node(), "coder")
    assert not satisfies(needs, _node(models=["apex-35b"]), "coder")


def test_role_requirement():
    needs = TaskNeeds(roles=["critic"])
    assert satisfies(needs, _node(), "critic")
    assert not satisfies(needs, _node(), "coder")


def test_empty_needs_object_is_unconstrained():
    assert satisfies(TaskNeeds(), _node(), "planner")
    # fail-closed still applies: a needs object with no constraints but set
    # requires a live roster entry.
    assert not satisfies(TaskNeeds(), None, "planner")


def test_merge_unions_roles_and_models():
    a = _node(roles=["coder"], models=["apex-35b"], load=0.4)
    b = _node(roles=["critic"], models=["gemma-4-31b"], load=0.9)
    merged = a.merge(b)
    assert merged.node_id == "n1"
    assert merged.roles == ["coder", "critic"]
    assert merged.models == ["apex-35b", "gemma-4-31b"]
    assert merged.load == 0.9
    assert merged.key() == "roster.n1"
