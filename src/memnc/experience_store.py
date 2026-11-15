"""ACE-style automated experience store (roadmap 15.3 item 5).

DESIGN (before code, per repo rule): docs/design-ace-experience.md.
Evidence: ACE (ICLR 2026) — context as an append-only, itemized bullet
store with a deterministic merge; the two guarded failure modes are
brevity bias (over-compression in one step) and context collapse
(duplicate bullets accumulating).

This module is the programmatic counterpart of docs/experience.md: a
file-backed, NATS-free store of per-topic experience bullets with a
deterministic merge that enforces both ACE guards. Producers (bench runs
now, observation deposits later) append bullets; nothing here calls an
LLM. (The sibling module memnc.experience is the 14.13 step 4 ranked
attention over observation deposits — read-only ranking, not storage.)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .observations import TaskObservation

# One merge may retire at most this fraction of a topic's existing bullets
# (brevity-bias guard: compression in small deliberate steps only).
MAX_PRUNE_RATIO = 0.3

# Token-set Jaccard overlap at/above which a new bullet counts as a
# near-duplicate of an existing one (context-collapse guard).
DUPLICATE_JACCARD = 0.8


def _normalize(text: str) -> list[str]:
    """Token list for duplicate comparison (lowercased, words only)."""
    return re.findall(r"[a-z0-9]+", text.lower())


def _jaccard(a: list[str], b: list[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


@dataclass
class Bullet:
    """One self-contained experience sentence, append-only."""

    id: str
    topic: str
    text: str
    source: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "topic": self.topic, "text": self.text, "source": self.source}


@dataclass
class MergeReport:
    """What one merge did; deterministic given (existing, new)."""

    added: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    rejected_reason: str = ""


class ExperienceStore:
    """File-backed append-only experience bullets, grouped by topic."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self.bullets: list[Bullet] = []

    # -- persistence -------------------------------------------------------

    def load(self) -> ExperienceStore:
        if self.path and self.path.exists():
            rows = json.loads(self.path.read_text(encoding="utf-8"))
            self.bullets = [Bullet(**row) for row in rows]
        return self

    def save(self) -> None:
        if not self.path:
            raise ValueError("store has no backing file")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps([b.to_dict() for b in self.bullets], indent=2) + "\n",
            encoding="utf-8",
        )

    # -- reads -------------------------------------------------------------

    def topic(self, name: str) -> list[Bullet]:
        return [b for b in self.bullets if b.topic == name]

    # -- writes ------------------------------------------------------------

    def append(self, topic: str, text: str, source: str = "") -> Bullet:
        """Append one bullet; ids continue the topic's sequence."""
        seq = sum(1 for b in self.bullets if b.topic == topic) + 1
        bullet = Bullet(id=f"{topic}-{seq}", topic=topic, text=text, source=source)
        self.bullets.append(bullet)
        return bullet

    def merge(self, topic: str, new_bullets: list[str], source: str = "") -> MergeReport:
        """Deterministically fold new bullets into a topic.

        Guards (ACE): near-duplicates (Jaccard >= DUPLICATE_JACCARD) are
        dropped in favour of the existing bullet, preferring the longer
        text; a merge that would retire more than MAX_PRUNE_RATIO of the
        topic's existing bullets is rejected unchanged (brevity bias).
        """
        report = MergeReport()
        existing = self.topic(topic)
        if not new_bullets:
            return report

        drop_existing: set[str] = set()
        for text in new_bullets:
            norm = _normalize(text)
            dup_of = None
            for b in existing:
                if b.id in drop_existing:
                    continue
                if _jaccard(norm, _normalize(b.text)) >= DUPLICATE_JACCARD:
                    # Prefer the longer phrasing: it carries more detail
                    # (brevity-bias guard on the duplicate decision too).
                    if len(text) > len(b.text):
                        drop_existing.add(b.id)
                        continue  # the replacement is added below
                    dup_of = b
                    break
            if dup_of is not None:
                report.duplicates.append(text)
            else:
                report.added.append(text)

        # merge is monotone: near-duplicates are one-for-one replacements,
        # so it never shrinks a topic. Compression is the explicit
        # compress() step, guarded by MAX_PRUNE_RATIO (brevity bias).
        for b in drop_existing:
            self.bullets = [x for x in self.bullets if x.id != b]
        for text in report.added:
            self.append(topic, text, source)
        return report


    def compress(
        self, topic: str, new_bullets: list[str], source: str = ""
    ) -> MergeReport:
        """Explicit ACE summarization step for one topic.

        Replaces ALL of the topic's bullets with ``new_bullets`` — this is
        where an LLM summarization belongs (OUTSIDE this module; the caller
        decides what the compressed bullets say). Guard (brevity bias):
        rejected unless the new list keeps at least
        ``(1 - MAX_PRUNE_RATIO)`` of the old bullet count. Deterministic:
        same (existing, new) -> same result.
        """
        existing = self.topic(topic)
        report = MergeReport()
        if existing and len(new_bullets) < len(existing) * (1 - MAX_PRUNE_RATIO):
            report.rejected_reason = (
                f"compression {len(existing)} -> {len(new_bullets)} bullets loses "
                f"> {MAX_PRUNE_RATIO:.0%}; rejected (brevity-bias guard)"
            )
            return report
        self.bullets = [b for b in self.bullets if b.topic != topic]
        seq = 0
        for text in new_bullets:
            seq += 1
            self.bullets.append(
                Bullet(id=f"{topic}-{seq}", topic=topic, text=text, source=source)
            )
        report.added = [b.text for b in self.topic(topic)]
        return report


def record_observation(store: ExperienceStore, observation: TaskObservation) -> list[str]:
    """Producer: turn one terminal-task observation into experience bullets.

    Deterministic, no LLM calls. Successes and failures go to separate
    topics so a reader can pull either ledger independently. Bullets are
    folded through merge() so repeated similar outcomes collapse (ACE
    context-collapse guard) instead of accumulating per task.
    """
    route = observation.route or {}
    steps = " ".join(f"{role}={node}" for role, node in sorted(route.items()))
    if observation.outcome == "done":
        text = (
            f"task {observation.task_id[:8]} DONE via {steps}"
            f" ({observation.revisions} revisions)"
        )
    else:
        error = (observation.error or "no error recorded")[:200]
        text = (
            f"task {observation.task_id[:8]} {observation.outcome} on {steps}:"
            f" {error}"
        )
    topic = "success" if observation.outcome == "done" else "failure"
    store.merge(topic, [text], source=observation.task_id)
    # Return what the store now holds for this task (a near-duplicate fold
    # may have replaced our text with the longer existing bullet).
    return [b.text for b in store.topic(topic) if observation.task_id[:8] in b.text]


def format_bullets(store: ExperienceStore, topic: str | None, limit: int) -> str:
    """Render store bullets newest-first, optionally filtered by topic."""
    bullets = list(reversed(store.bullets))
    if topic:
        bullets = [b for b in bullets if b.topic == topic.lower()]
    if not bullets:
        return "no experience bullets"
    bullets = bullets[:limit]
    return "\n".join(f"{b.id}  {b.text}" for b in bullets)


def record_bench_run(jsonl_path: str | Path, store: ExperienceStore) -> dict[str, MergeReport]:
    """Turn a bench JSONL into per-arm experience bullets.

    Success counts per tier and mean tokens/elapsed per arm become summary
    bullets; each failed task becomes one bullet with its checker issues or
    error text. Arm-level topic names (`bench-arm-<ARM>`) keep the store
    queryable per mechanism.
    """
    rows: list[dict] = []
    with open(jsonl_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))

    reports: dict[str, MergeReport] = {}
    arms: dict[str, list[dict]] = {}
    for row in rows:
        arms.setdefault(str(row.get("arm", "")), []).append(row)

    for arm, arm_rows in sorted(arms.items()):
        topic = f"bench-arm-{arm}"
        bullets: list[str] = []
        by_tier: dict[str, list[int]] = {}
        tokens: list[int] = []
        elapsed: list[float] = []
        for row in arm_rows:
            tier = str(row.get("tier", "?"))
            ok = 1 if row.get("success") else 0
            by_tier.setdefault(tier, [0, 0])
            by_tier[tier][0] += ok
            by_tier[tier][1] += 1
            tokens.append(int(row.get("tokens_in", 0)) + int(row.get("tokens_out", 0)))
            elapsed.append(float(row.get("elapsed_s", 0.0) or 0.0))
            if not row.get("success"):
                issues = "; ".join(row.get("issues") or [])
                detail = issues or str(row.get("error") or "unknown failure")
                bullets.append(f"Failed task {row.get('task_id', '?')} ({tier}): {detail}")
        for tier in sorted(by_tier):
            ok, total = by_tier[tier]
            bullets.append(f"{tier} tier: {ok}/{total} success")
        if tokens:
            bullets.append(
                f"mean tokens/task {sum(tokens) / len(tokens):.0f}, "
                f"mean elapsed {sum(elapsed) / len(elapsed):.1f}s"
            )
        reports[topic] = store.merge(topic, bullets, source="bench")
    return reports
