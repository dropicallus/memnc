"""Roadmap board: parse the Part I rows, render the short board, change a task's state.

The Part I rows of docs/roadmap.md are the source of truth. The board block at the
top of the roadmap is generated from them. States follow the A2A task lifecycle
(v1.0, TASK_STATE_*) where one exists. DEFERRED has no A2A equivalent: the task is
kept but not scheduled.
"""

from __future__ import annotations

import re
from pathlib import Path

ROADMAP = Path(__file__).resolve().parents[2] / "docs" / "roadmap.md"
START = "<!-- board:start -->"
END = "<!-- board:end -->"

GLYPH_TO_STATE = {"☐": "SUBMITTED", "🟡": "WORKING", "✅": "COMPLETED", "🗑": "CANCELED", "⏸": "DEFERRED"}
STATE_TO_GLYPH = {state: glyph for glyph, state in GLYPH_TO_STATE.items()}
STATES = tuple(STATE_TO_GLYPH)
PRIORITIES = ("P0", "P1", "P2")
ROW = re.compile(r"^\| (T\d+\.\d+) \| (.+?) \| (☐|🟡|✅|🗑|⏸) \| (.*) \|$")
PRIORITY = re.compile(r"\*\*(P[0-2])\*\*")
OPEN = {"SUBMITTED", "WORKING"}


def _part_one(text: str) -> str:
    return text.split("## Part I", 1)[1].split("## Part II", 1)[0]


def tasks(text: str) -> list[tuple[str, str, str, str]]:
    """(id, title, state, priority) for every Part I row, in document order."""
    found = []
    for line in _part_one(text).splitlines():
        match = ROW.match(line)
        if not match:
            continue
        task_id, title, glyph, evidence = match.groups()
        prio = PRIORITY.search(evidence)
        found.append((task_id, title.replace("`", "").strip(), GLYPH_TO_STATE[glyph], prio.group(1) if prio else ""))
    return found


def get_task(text: str, task_id: str) -> dict:
    """One task as a plain dict; raises KeyError when the id is not in Part I."""
    for line in _part_one(text).splitlines():
        match = ROW.match(line)
        if match and match.group(1) == task_id:
            _id, title, glyph, evidence = match.groups()
            prio = PRIORITY.search(evidence)
            return {
                "id": task_id,
                "title": title.replace("`", "").strip(),
                "state": GLYPH_TO_STATE[glyph],
                "priority": prio.group(1) if prio else "",
                "evidence": evidence,
            }
    raise KeyError(task_id)


def set_state(text: str, task_id: str, state: str) -> str:
    """Return the roadmap text with the task's status glyph changed. Only Part I is touched."""
    if state not in STATE_TO_GLYPH:
        raise ValueError(f"unknown state {state!r}; use one of {', '.join(STATES)}")
    get_task(text, task_id)  # raises KeyError for an unknown id
    head, rest = text.split("## Part I", 1)
    part_one, tail = rest.split("## Part II", 1)
    lines = part_one.split("\n")
    changed = False
    for index, line in enumerate(lines):
        match = ROW.match(line)
        if match and match.group(1) == task_id:
            task_id_, title, _glyph, evidence = match.groups()
            lines[index] = f"| {task_id_} | {title} | {STATE_TO_GLYPH[state]} | {evidence} |"
            changed = True
            break
    if not changed:  # pragma: no cover - get_task already found the row
        raise KeyError(task_id)
    return head + "## Part I" + "\n".join(lines) + "## Part II" + tail


def render(text: str) -> str:
    items = tasks(text)
    lines = [
        START,
        "",
        "## Board",
        "",
        "Generated from the Part I tables by `scripts/board.py`. Edit the rows, not this block.",
        "",
        "- `SUBMITTED` planned · `WORKING` in progress · `COMPLETED` done and verified",
        "- `CANCELED` dropped · `DEFERRED` kept, not scheduled (no A2A equivalent)",
        "",
    ]
    sections = [
        ("P0 — now", lambda s, p: s in OPEN and p == "P0"),
        ("P1 — next", lambda s, p: s in OPEN and p == "P1"),
        ("P2 — later", lambda s, p: s in OPEN and p == "P2"),
        ("Unranked", lambda s, p: s in OPEN and p == ""),
        ("Deferred", lambda s, p: s == "DEFERRED"),
    ]
    for label, pick in sections:
        rows = [item for item in items if pick(item[2], item[3])]
        lines.append(f"### {label} ({len(rows)})")
        lines.append("")
        if rows:
            lines.append("| ID | Task | State |")
            lines.append("|---|---|---|")
            for task_id, title, state, _ in rows:
                short = title if len(title) <= 90 else title[:87] + "..."
                lines.append(f"| {task_id} | {short} | {state} |")
        else:
            lines.append("None.")
        lines.append("")
    done = [item[0] for item in items if item[2] == "COMPLETED"]
    dropped = [item[0] for item in items if item[2] == "CANCELED"]
    lines.append(f"### Completed ({len(done)})")
    lines.append("")
    lines.append(", ".join(done) if done else "None.")
    lines.append("")
    lines.append(f"### Canceled ({len(dropped)})")
    lines.append("")
    lines.append(", ".join(dropped) if dropped else "None.")
    lines.append("")
    lines.append(END)
    return "\n".join(lines)


def updated(text: str) -> str:
    """The roadmap text with its board block regenerated (inserted before Contents if absent)."""
    block = render(text)
    if START in text and END in text:
        pattern = re.escape(START) + r".*?" + re.escape(END)
        return re.sub(pattern, lambda _m: block, text, count=1, flags=re.S)
    return text.replace("## Contents", block + "\n\n## Contents", 1)
