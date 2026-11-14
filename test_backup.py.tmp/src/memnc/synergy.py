"""14.13 step 3: team-synergy aggregation (measurement only).

Pure computation over deposited observations: which model pairs served
together, how often, with what success ratio and revision cost. No
routing decisions derive from these numbers yet -- measure first, act
later (plan 14.13 rule of thumb).
"""

from __future__ import annotations

from memnc.observations import TaskObservation


def compute_synergy_metrics(observations: list[TaskObservation]) -> dict[str, dict]:
    """Aggregate per model-pair: total, successes, ratio, mean revisions.

    Pairs come from TaskObservation.synergy_pairs() (unordered, no
    self-edges). Failed and cancelled tasks count in the total but not
    as successes. Result is sorted by success ratio (desc), then pair id.
    """
    stats: dict[str, dict] = {}
    for obs in observations:
        success = 1 if obs.outcome == "done" else 0
        for pair in obs.synergy_pairs():
            entry = stats.setdefault(
                pair,
                {"total": 0, "successes": 0, "revisions": 0},
            )
            entry["total"] += 1
            entry["successes"] += success
            entry["revisions"] += obs.revisions
    out: dict[str, dict] = {}
    for pair, entry in stats.items():
        total = entry["total"]
        out[pair] = {
            "total": total,
            "successes": entry["successes"],
            "success_ratio": entry["successes"] / total,
            "mean_revisions": entry["revisions"] / total,
        }
    return dict(sorted(out.items(), key=lambda kv: (-kv[1]["success_ratio"], kv[0])))


def format_synergy(metrics: dict[str, dict], limit: int = 10) -> str:
    """Human-readable table for the synergy metrics (meshctl stats)."""
    if not metrics:
        return "no model pairs observed yet"
    lines = ["pair | n | success | ratio | mean_rev"]
    for pair, m in list(metrics.items())[:limit]:
        a, b = pair.split("|+|")
        lines.append(
            f"{a} + {b} | {m['total']} | {m['successes']} "
            f"| {m['success_ratio']:.2f} | {m['mean_revisions']:.2f}"
        )
    return "\n".join(lines)
