"""15.4.3 attempt journal: per-attempt checkpoint notes carried across retries.

Design (docs/roadmap.md 15.4.3): every failed attempt that schedules a retry
appends one bounded entry to the snapshot, recording what the attempt
completed (stage, produced files, implementation summary) and where it
failed. The next attempt's prompt renders these notes so planner/coder/critic
resume from the last good state instead of re-deriving it from the error
string alone. Entries are capped (count and per-field length) so the KV
snapshot cannot grow unboundedly.
"""

from __future__ import annotations

from typing import Any

from .failclass import UNKNOWN, classify_failure

MAX_ENTRIES = 8
MAX_ERROR_CHARS = 400
MAX_SUMMARY_CHARS = 200
MAX_FILES = 20


def note_failure(
    snapshot: Any,
    event: Any,
    result: Any,
    role: Any = None,
) -> None:
    """Append a journal entry when a failed attempt schedules a retry.

    No-op on success, on events without a retry (attempt_delta <= 0) and on
    any snapshot type that lacks the journal field (FakeBuses and older
    snapshots keep working). Call BEFORE the caller applies attempt_delta,
    so the entry records the attempt number that actually failed.
    """
    if event.ok or result.attempt_delta <= 0:
        return
    journal = getattr(snapshot, "attempt_journal", None)
    if journal is None:
        return
    code = getattr(snapshot, "code", None)
    files: list[str] = []
    summary = ""
    if code is not None:
        files = [getattr(f, "path", "") for f in getattr(code, "files", []) or []]
        files = [p for p in files if p][:MAX_FILES]
        summary = (getattr(code, "summary", "") or "")[:MAX_SUMMARY_CHARS]
    entry = {
        "attempt": snapshot.attempt,
        "stage": getattr(role, "value", "") or "",
        "kind": classify_failure(event.reason),
        "error": (event.reason or "")[:MAX_ERROR_CHARS],
        "files": files,
        "summary": summary,
    }
    journal.append(entry)
    del journal[:-MAX_ENTRIES]


def render(entries: list[dict[str, Any]] | None) -> str:
    """Render journal entries as a prompt block; empty string when none."""
    if not entries:
        return ""
    lines = ["PROGRESS FROM PRIOR ATTEMPTS:"]
    for entry in entries:
        stage = entry.get("stage") or "unknown-stage"
        parts = [f"attempt {entry.get('attempt', '?')} failed at {stage}"]
        error = entry.get("error") or ""
        if error:
            kind = entry.get("kind") or ""
            label = f" ({kind})" if kind and kind != UNKNOWN else ""
            parts.append(f"error{label}: {error}")
        summary = entry.get("summary") or ""
        if summary:
            parts.append(f"implementation so far: {summary}")
        files = entry.get("files") or []
        if files:
            parts.append("files present: " + ", ".join(files))
        lines.append("- " + "; ".join(parts))
    return "\n".join(lines)
