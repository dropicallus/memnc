"""Weak->strong routing on the M14 claim path (prefer_node binding).

Slice 14.15.6: the claim loop must (a) never let a non-preferred node claim
a snapshot that carries prefer_node, and (b) let the preferred node claim
even when the synergy gate would withdraw — the driver deliberately picked
that node, so experience-based step-back must not veto explicit routing.
"""

from memnc.schema import TaskSnapshot
from memnc.worker import Worker


def _snap(prefer: str = "") -> TaskSnapshot:
    return TaskSnapshot(
        task_id="t1",
        trace_id="tr1",
        task="do it",
        claimable=True,
        next_role="planner",
        prefer_node=prefer,
    )


def test_non_preferred_node_cannot_claim():
    assert not Worker._claim_permitted(_snap("node-a"), "node-c-planner", False)


def test_preferred_node_may_claim_despite_withdraw():
    # prefix match: worker node_names are role-suffixed
    assert Worker._claim_permitted(_snap("node-a"), "node-a-planner", True)


def test_no_preference_follows_synergy_gate():
    # withdrawn -> nothing unbound is claimable
    assert not Worker._claim_permitted(_snap(""), "node-a-planner", True)
    # not withdrawn -> claimable
    assert Worker._claim_permitted(_snap(""), "node-a-planner", False)


def test_role_suffix_counts_as_host_match():
    assert Worker._claim_permitted(_snap("node-a"), "node-a-coder", False)
