"""14.13 Step 6: dreaming — deterministic consolidation over observations.

Pure-logic core of the nightly dreaming job (design note in
docs/roadmap.md, "Step 6 design"): cluster deposited observations by
task-text similarity, emit task-pattern bullets, sweep for superseded
route preferences, and summarize the current synergy rankings.

Deterministic only: no model calls, no RNG, no wall-clock reads in the
core functions (``now`` is injectable). Consolidation, never deletion —
the only output is bullet texts for the experience store's monotone
``merge()``; nothing here removes observations or shrinks the store.
"""

from __future__ import annotations

from collections import Counter

from memnc.experience import (
    _CORROB_PAIR_THRESHOLD,
    jaccard,
    tokenize,
)
from memnc.observations import TaskObservation
from memnc.pheromone import task_type_from_prompt
from memnc.synergy import compute_synergy_metrics

# A cluster must hold at least this many observations before a pattern
# bullet is worth emitting (a single run is not a pattern).
MIN_CLUSTER_SIZE = 2
# Synergy pairs below this run count are too thin to rank on.
MIN_RANKING_RUNS = 2
TOP_RANKING_PAIRS = 3

DREAM_TOPIC = "dream"
RANKING_TOPIC = "rankings"


def cluster_observations(
    observations: list[TaskObservation],
    threshold: float = _CORROB_PAIR_THRESHOLD,
) -> list[list[TaskObservation]]:
    """Greedy, deterministic clustering of observations by task similarity.

    Observations are visited in (task_id, observed_at) order; each joins
    the first cluster whose representative task text reaches
    ``threshold`` similarity, otherwise seeds a new cluster. The
    representative is the cluster's first member, so the result is
    stable regardless of input order.
    """
    ordered = sorted(observations, key=lambda o: (o.task_id, o.observed_at))
    reps: list[set[str]] = []
    clusters: list[list[TaskObservation]] = []
    for obs in ordered:
        tokens = tokenize(obs.task)
        for rep, cluster in zip(reps, clusters, strict=False):
            if jaccard(rep, tokens) >= threshold:
                cluster.append(obs)
                break
        else:
            reps.append(tokens)
            clusters.append([obs])
    return clusters


def _majority(counter: Counter, key: str) -> str | None:
    """Deterministic majority pick: count desc, then value asc."""
    if not counter:
        return None
    top = max(counter.values())
    return sorted(v for v, c in counter.items() if c == top)[0]


def pattern_bullet(cluster: list[TaskObservation]) -> str | None:
    """One deterministic task-pattern bullet for a cluster, or None."""
    if len(cluster) < MIN_CLUSTER_SIZE:
        return None
    task_type = task_type_from_prompt(cluster[0].task)
    total = len(cluster)
    done = [o for o in cluster if o.outcome == "done"]
    rate = len(done) / total
    lines = [f"pattern {task_type}: {len(done)}/{total} runs done ({rate:.0%})"]
    if done:
        routes = Counter(
            " ".join(f"{r}={v.split(':')[-1]}" for r, v in sorted(o.route.items()))
            for o in done
        )
        best = _majority(routes, task_type)
        lines.append(f"best route: {best}")
        rev = sum(o.revisions for o in done) / len(done)
        if rev >= 1:
            lines.append(f"mean revisions on done: {rev:.1f}")
    failed = [o for o in cluster if o.outcome != "done"]
    if failed:
        causes = Counter((o.error or "unknown error").splitlines()[0][:120] for o in failed)
        lines.append(f"dominant failure: {_majority(causes, task_type)}")
    return "; ".join(lines)


def supersession_bullets(cluster: list[TaskObservation]) -> list[str]:
    """Detect a flipped best route inside one cluster over time.

    The cluster is split at its median observation time; if the majority
    successful route of the newer half differs from the older half's,
    a supersession bullet records the flip (old bullet stays).
    """
    if len(cluster) < MIN_CLUSTER_SIZE:
        return []
    ts = sorted(o.observed_at for o in cluster)
    mid = ts[len(ts) // 2]
    older = [o for o in cluster if o.observed_at < mid]
    newer = [o for o in cluster if o.observed_at >= mid]
    if not older or not newer:
        return []

    def best_route(obs_list: list[TaskObservation]) -> str | None:
        done = [o for o in obs_list if o.outcome == "done"]
        if not done:
            return None
        routes = Counter(
            " ".join(f"{r}={v.split(':')[-1]}" for r, v in sorted(o.route.items()))
            for o in done
        )
        return _majority(routes, "route")

    old_route = best_route(older)
    new_route = best_route(newer)
    if old_route is None or new_route is None or old_route == new_route:
        return []
    task_type = task_type_from_prompt(cluster[0].task)
    return [
        f"pattern {task_type}: preferred route now {new_route} "
        f"(supersedes {old_route})"
    ]


def ranking_bullet(observations: list[TaskObservation]) -> str | None:
    """Summarize the top model-pair rankings, or None if too thin."""
    metrics = compute_synergy_metrics(observations)
    qualified = [
        (pair, m)
        for pair, m in metrics.items()
        if m["total"] >= MIN_RANKING_RUNS and m["successes"] > 0
    ]
    if not qualified:
        return None
    top = qualified[:TOP_RANKING_PAIRS]
    parts = [
        f"{pair} {m['successes']}/{m['total']}" for pair, m in top
    ]
    return "top team pairs: " + ", ".join(parts)


def dream_bullets(
    observations: list[TaskObservation],
    cluster_threshold: float = _CORROB_PAIR_THRESHOLD,
) -> dict[str, list[str]]:
    """Full dreaming pass: cluster -> patterns + supersessions + rankings.

    Returns {topic: [bullet texts]} ready for ExperienceStore.merge().
    Empty clusters beyond the size floor simply contribute nothing, so an
    idle mesh produces an empty merge (the store is never touched with
    nothing to say).
    """
    clusters = cluster_observations(observations, cluster_threshold)
    out: dict[str, list[str]] = {DREAM_TOPIC: [], RANKING_TOPIC: []}
    for cluster in clusters:
        bullet = pattern_bullet(cluster)
        if bullet:
            out[DREAM_TOPIC].append(bullet)
        out[DREAM_TOPIC].extend(supersession_bullets(cluster))
    ranking = ranking_bullet(observations)
    if ranking:
        out[RANKING_TOPIC].append(ranking)
    return {topic: texts for topic, texts in out.items() if texts}
