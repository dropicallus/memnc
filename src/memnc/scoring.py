from dataclasses import dataclass


@dataclass(frozen=True)
class Candidate:
    """A candidate for utility scoring in SwarmHarness."""

    agent_id: str
    skill: str
    capacity: int
    load: float
    latency: float
    trust: float


def score_candidates(
    candidates: list[Candidate], weights: dict[str, float] | None = None
) -> list[tuple[str, float]]:
    """
    Calculates utility scores for a list of candidates based on a weighted sum of
    normalized features.

    Formula: U = w1*cap + w2*(1-load) + w3*(1-lat) + w4*trust

    Normalization:
    - capacity: min(cap, ...) / max_cap in group
    - load: already 0..1
    - latency: 1.0 - (min(lat, lat_cap) / lat_cap) where lat_cap = 5.0
    - trust: trust / TRUST_MAX (where max is 10.0)

    :param candidates: List of Candidate objects.
    :param weights: Optional dictionary of weights for 'cap', 'load', 'lat', 'trust'.
    :return: Sorted list of (agent_id, score) descending.
    """
    if not candidates:
        return []

    w = weights or {"cap": 1.0, "load": 1.0, "lat": 1.0, "trust": 1.0}

    max_cap = max((c.capacity for c in candidates), default=1)
    lat_cap = 5.0
    TRUST_MAX = 10.0

    scored = []
    for c in candidates:
        norm_cap = c.capacity / max_cap if max_cap > 0 else 0
        norm_load = 1.0 - c.load
        norm_lat = 1.0 - (min(c.latency, lat_cap) / lat_cap)
        norm_trust = c.trust / TRUST_MAX

        score = (
            w.get("cap", 1.0) * norm_cap
            + w.get("load", 1.0) * norm_load
            + w.get("lat", 1.0) * norm_lat
            + w.get("trust", 1.0) * norm_trust
        )
        scored.append((c.agent_id, score))

    return sorted(scored, key=lambda x: x[1], reverse=True)


def router_candidates(nodes, trust: dict[str, float]):
    """M8: build SwarmHarness Candidates from T6 roster + trust ledger.

    Pure function so the live router can be unit-tested without NATS.
    Paused nodes are skipped (human pause wins over any utility score).
    """

    out = []
    for n in nodes:
        if n.paused:
            continue
        out.append(
            Candidate(
                agent_id=n.node_id,
                skill=",".join(n.roles) or "any",
                capacity=max(1, int(n.vram_gb or 1)),
                load=min(1.0, max(0.0, n.load or 0.0)),
                latency=0.0,
                trust=trust.get(n.node_id, 0.0),
            )
        )
    return out


def signed_attribution(snapshot) -> str:
    """M8: one ledger entry = task, submitting node (the signature) + claimer."""
    return (
        f"task_{snapshot.task_id}"
        f":submit={getattr(snapshot, 'origin_node', '')}"
        f":claim={snapshot.claimed_by}"
    )
