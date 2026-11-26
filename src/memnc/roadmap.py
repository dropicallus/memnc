"""Roadmap -> backlog sync.

The roadmap document is the map: epics with a table of tasks and a status
glyph per row. The backlog is the queue. Keeping them in sync by hand means
every open row has to be retyped as a task prompt, which is exactly where
work gets lost.

This module parses the roadmap tables and turns the *open* rows (``☐``
planned, ``🟡`` in progress) into backlog tasks. The sync is idempotent: a
task already in the backlog is recognised by the ``[T<id>]`` marker in its
title, so running it twice adds nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# status glyphs used in docs/roadmap.md
TODO = "☐"
WIP = "🟡"
DONE = "✅"
OPEN_STATUSES = {TODO, WIP}

_ROW = re.compile(
    r"^\|\s*(T[\w.]+)\s*\|\s*(.+?)\s*\|\s*(✅|🟡|☐)\s*\|\s*(.*?)\s*\|\s*$"
)
_EPIC = re.compile(r"^##\s+(E\d+)\s+(.+?)\s*$")


@dataclass(frozen=True)
class RoadmapTask:
    """One open table row of the roadmap."""

    task_id: str  # T1.6
    title: str
    status: str  # ☐ or 🟡
    evidence: str
    epic: str  # "E1 Substrate and mesh core"

    @property
    def marker(self) -> str:
        return f"[{self.task_id}]"

    def backlog_title(self) -> str:
        return f"{self.marker} {self.title}"

    def backlog_prompt(self) -> str:
        parts = [f"Roadmap {self.epic}: {self.title}"]
        if self.evidence:
            parts.append(f"Context from the roadmap: {self.evidence}")
        parts.append(
            "Read docs/roadmap.md first for this task's section and implement it. "
            "Change only what this task requires."
        )
        return "\n".join(parts)


def parse_roadmap(text: str) -> list[RoadmapTask]:
    """Every roadmap table row, in document order."""
    tasks: list[RoadmapTask] = []
    epic = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        epic_match = _EPIC.match(line)
        if epic_match:
            epic = f"{epic_match.group(1)} {epic_match.group(2)}"
            continue
        row = _ROW.match(line)
        if row is None:
            continue
        tasks.append(
            RoadmapTask(
                task_id=row.group(1),
                title=row.group(2).replace("`", "").strip(),
                status=row.group(3),
                evidence=row.group(4).replace("`", "").strip(),
                epic=epic,
            )
        )
    return tasks


def open_tasks(text: str) -> list[RoadmapTask]:
    """Rows that still need work (☐ planned, 🟡 in progress)."""
    return [t for t in parse_roadmap(text) if t.status in OPEN_STATUSES]


def missing(rows: list[RoadmapTask], existing_titles: list[str]) -> list[RoadmapTask]:
    """Rows whose marker does not appear in any existing backlog title."""
    seen = {title for title in existing_titles}
    return [row for row in rows if not any(row.marker in t for t in seen)]
