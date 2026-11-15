"""DPO pair generator over bench runs (roadmap 15.3 item 6, Flow-DPO).

Evidence: Flow-DPO-style preference learning needs (chosen, rejected)
solution pairs for the SAME task. The bench JSONL + deterministic checkers
provide exactly that label: a run that passes the checker is chosen, a run
that fails it is rejected — no human labels involved.

Pairs are built from rows that carry `solutions` (recorded when the bench
engine runs with record_solutions=True; see runner.RunResult). Selection
is deterministic: the chosen row is the successful run with the FEWEST
total tokens (tie-break: lowest run_id) — the cheapest correct solution;
the rejected row is the failed run with the MOST tokens (tie-break:
lowest run_id). The learned verifier (deliverable) trains on these pairs
and is a later, separate slice.
"""

from __future__ import annotations

import json
from pathlib import Path


def _total_tokens(row: dict) -> int:
    return int(row.get("tokens_in", 0)) + int(row.get("tokens_out", 0))


def build_pairs(rows: list[dict], prompts: dict[str, str] | None = None) -> list[dict]:
    """One pair per task that has both a passing and a failing solution."""
    prompts = prompts or {}
    by_task: dict[str, list[dict]] = {}
    for row in rows:
        if row.get("solutions"):
            by_task.setdefault(str(row.get("task_id", "")), []).append(row)

    pairs: list[dict] = []
    for task_id, task_rows in sorted(by_task.items()):
        wins = [r for r in task_rows if r.get("success")]
        losses = [r for r in task_rows if not r.get("success")]
        if not wins or not losses:
            continue
        chosen = min(wins, key=lambda r: (_total_tokens(r), str(r.get("run_id", ""))))
        # Max tokens; ties resolved to the lowest run_id.
        rejected = min(
            losses, key=lambda r: (-_total_tokens(r), str(r.get("run_id", "")))
        )
        pairs.append(
            {
                "task_id": task_id,
                "tier": chosen.get("tier", ""),
                "prompt": prompts.get(task_id, ""),
                "chosen": chosen["solutions"],
                "rejected": rejected["solutions"],
                "chosen_run_id": chosen.get("run_id", ""),
                "rejected_run_id": rejected.get("run_id", ""),
                "chosen_arm": chosen.get("arm", ""),
                "rejected_arm": rejected.get("arm", ""),
            }
        )
    return pairs


def load_rows(jsonl_path: str | Path) -> list[dict]:
    rows: list[dict] = []
    with open(jsonl_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def write_pairs(pairs: list[dict], path: str | Path) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for pair in pairs:
            fh.write(json.dumps(pair) + "\n")
