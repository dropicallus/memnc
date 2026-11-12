"""M8: NATS-free unit tests for the live router + signed attribution."""

from memnc.schema import NodeInfo, TaskSnapshot
from memnc.scoring import router_candidates, signed_attribution


def _node(node_id, *, vram=16.0, load=0.0, paused=False, roles=None):  # leakscan:ok
    return NodeInfo(
        node_id=node_id,
        roles=roles or ["coder"],
        vram_gb=vram,
        load=load,
        paused=paused,
    )


def test_router_candidates_maps_roster_and_trust():
    nodes = [_node("a", vram=32.0, load=0.5), _node("b", vram=16.0)]  # leakscan:ok
    trust = {"a": 6.0}
    cands = router_candidates(nodes, trust)
    assert [c.agent_id for c in cands] == ["a", "b"]
    a = cands[0]
    assert a.capacity == 32
    assert a.load == 0.5
    assert a.trust == 6.0
    assert cands[1].trust == 0.0  # missing ledger entry defaults to 0
    assert cands[1].skill == "coder"


def test_router_candidates_skips_paused_nodes():
    nodes = [_node("a", paused=True), _node("b")]
    assert [c.agent_id for c in router_candidates(nodes, {})] == ["b"]


def test_router_candidates_clamps_load():
    nodes = [_node("a", load=5.0), _node("b", load=-1.0)]
    loads = {c.agent_id: c.load for c in router_candidates(nodes, {})}
    assert loads == {"a": 1.0, "b": 0.0}


def test_signed_attribution_carries_task_submitter_claimer():
    snap = TaskSnapshot(
        task_id="t1", trace_id="tr1", task="do", origin_node="node-b"
    )
    snap.claimed_by = "node-a"
    entry = signed_attribution(snap)
    assert entry == "task_t1:submit=node-b:claim=node-a"
