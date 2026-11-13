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


def compute_server_success(observations: list[TaskObservation]) -> dict[str, dict]:
    """14.13 step 5 (read side): per-server track record.

    A "server" is one route value, i.e. "node-role:model" as recorded on
    the snapshot (e.g. "node-a-planner:qwen2.5:7b"). Aggregates total,
    successes (outcome "done") and the success ratio per server. This is
    the per-executor cut of the same observations the pair-level
    compute_synergy_metrics() aggregates — same KV, different grouping.
    """
    stats: dict[str, dict] = {}
    for obs in observations:
        success = 1 if obs.outcome == "done" else 0
        for server in obs.route.values():
            entry = stats.setdefault(server, {"total": 0, "successes": 0})
            entry["total"] += 1
            entry["successes"] += success
    out: dict[str, dict] = {}
    for server, entry in stats.items():
        total = entry["total"]
        out[server] = {
            "total": total,
            "successes": entry["successes"],
            "success_ratio": entry["successes"] / total,
        }
    return out


def should_withdraw(
    server_stats: dict[str, dict],
    server: str,
    min_samples: int = 3,
    min_ratio: float = 0.34,
) -> bool:
    """14.13 step 5 (decide side): should this server withdraw from claiming?

    Self-organized claim gate: a server whose own track record is below
    min_ratio (with at least min_samples of evidence) stops claiming that
    role, leaving work to servers with a better history. Fail-closed to
    False (no withdrawal) when evidence is thin — measurement first, and
    a single unlucky failure never benches a server.

    This is experience-driven routing feedback (human-gated via config:
    the gate is a setting, and withdrawal is visible in the claim logs).
    """
    entry = server_stats.get(server)
    if entry is None:
        return False
    if entry["total"] < min_samples:
        return False
    return entry["success_ratio"] < min_ratio
