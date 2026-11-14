"""14.13 step 4: similarity + ranked attention over the observation ledger.

Port of the deterministic ranked attention (half-life decay + outcome
reward + corroboration) from the user's earlier context-attention engine
onto TaskObservation deposits. Jaccard similarity over task text is the
first basis (embedding upgrade stays open). Read-only: ranking never
mutates the ledger, measurement first.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from memnc.observations import TaskObservation

# Small English stopword set — enough to keep Jaccard informative on
# short task prompts without pulling in a dependency.
_STOPWORDS = frozenset(
    """a an and are as at be but by for from has have in into is it its
    of on or that the this to was were will with without""".split()
)

# Terminal outcomes ranked by how much evidence they carry. A failure is
# still evidence (avoid that route); a cancelled task carries none.
_OUTCOME_REWARD = {"done": 1.0, "failed": 0.25, "cancelled": 0.0}

# Corroboration: each other sufficiently-similar observation boosts the
# score, capped so a duplicate-heavy ledger cannot swamp unique evidence.
_CORROB_BOOST = 0.2
_CORROB_CAP = 2.0
_CORROB_PAIR_THRESHOLD = 0.5

_WORD_RE = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> set[str]:
    """Lowercase word tokens, stopwords and short tokens dropped."""
    return {
        tok
        for tok in _WORD_RE.findall(text.lower())
        if len(tok) >= 3 and tok not in _STOPWORDS
    }


def jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard similarity of two token sets (0.0 on empty either side)."""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / len(a | b)


def observation_similarity(obs: TaskObservation, query_tokens: set[str]) -> float:
    """Similarity between a query and a deposited observation's task text."""
    return jaccard(tokenize(obs.task), query_tokens)


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None


def ranked_attention(
    query: str,
    observations: list[TaskObservation],
    now: datetime | None = None,
    half_life_days: float = 14.0,
    min_similarity: float = 0.05,
) -> list[dict]:
    """Rank observations against a query by attention score.

    score = similarity * decay * outcome_reward * corroboration, where
    decay = 2 ** (-age_days / half_life_days). Deterministic: no RNG, no
    wall-clock reads (``now`` is injectable for tests). Returns a list of
    dicts (observation, similarity, age_days, score) sorted by score
    descending, similarity descending, task_id ascending for stability.
    Observations with similarity below min_similarity are dropped.
    """
    query_tokens = tokenize(query)
    now = now or datetime.now(UTC)
    scored: list[dict] = []
    for obs in observations:
        sim = observation_similarity(obs, query_tokens)
        if sim < min_similarity:
            continue
        ts = _parse_ts(obs.observed_at)
        age_days = 0.0
        decay = 1.0
        if ts is not None:
            age_days = max(0.0, (now - ts).total_seconds() / 86400.0)
            if half_life_days > 0:
                decay = 2.0 ** (-age_days / half_life_days)
        reward = _OUTCOME_REWARD.get(obs.outcome, 0.0)
        if reward <= 0.0:
            continue  # cancelled (or unknown outcome) carries no evidence
        scored.append(
            {
                "observation": obs,
                "similarity": sim,
                "age_days": age_days,
                "score": sim * decay * reward,
            }
        )
    # Corroboration pass: similar observations reinforce each other.
    task_tokens = [tokenize(item["observation"].task) for item in scored]
    for i, item in enumerate(scored):
        allies = sum(
            1
            for j, other in enumerate(task_tokens)
            if j != i and jaccard(task_tokens[i], other) >= _CORROB_PAIR_THRESHOLD
        )
        item["score"] *= min(1.0 + _CORROB_BOOST * allies, _CORROB_CAP)
    scored.sort(
        key=lambda item: (-item["score"], -item["similarity"], item["observation"].task_id)
    )
    return scored


def format_ranked(results: list[dict], limit: int = 10) -> str:
    """Human table for ranked attention results (meshctl similar)."""
    if not results:
        return "no similar observations"
    lines = ["score | sim | age(d) | outcome | task_id | task"]
    for item in results[:limit]:
        obs = item["observation"]
        lines.append(
            f"{item['score']:.3f} | {item['similarity']:.2f} "
            f"| {item['age_days']:.1f} | {obs.outcome} | {obs.task_id} "
            f"| {obs.task[:60]}"
        )
    return "\n".join(lines)
