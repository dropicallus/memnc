"""Workspace-state helpers shared by harnesses and their callers.

The harness contract is workspace state: after ``run`` the git diff of the
workspace IS the change. These helpers turn that state into file artifacts
for the ResultPayload contract.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

_GIT_TIMEOUT_S = 30


def _is_git_repo(workspace: Path) -> bool:
    return (workspace / ".git").exists()


async def _git(*args: str, cwd: Path) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_GIT_TIMEOUT_S)
    return stdout.decode(errors="replace")


async def changed_file_contents(
    workspace: Path, max_bytes: int = 1_000_000
) -> dict[str, str]:
    """Read the current content of every file changed in ``workspace``.

    ``git add -N .`` marks untracked files intent-to-add so ``git diff``
    reports them; it stages nothing. Deleted files are skipped (FileArtifact
    cannot express deletions) and files larger than ``max_bytes`` are
    truncated. Returns {} when the workspace is not a git repo.
    """
    if not _is_git_repo(workspace):
        return {}
    try:
        await _git("add", "-N", ".", cwd=workspace)
        names = await _git("diff", "--name-only", cwd=workspace)
    except (TimeoutError, FileNotFoundError, OSError):
        return {}
    changed: dict[str, str] = {}
    for line in names.splitlines():
        rel = line.strip()
        if not rel or ".." in rel or rel.startswith("/"):
            continue
        path = workspace / rel
        try:
            if not path.is_file():
                continue
            data = path.read_bytes()[:max_bytes]
        except OSError:
            continue
        changed[rel] = data.decode(errors="replace")
    return changed


def workspace_listing(root: Path, limit: int = 25) -> str | None:
    """15.4.3 workspace continuity: top-level listing for retry dispatch.

    Returns None when the root does not exist or is empty (nothing for a
    retrying planner/coder to know about). Otherwise a short deterministic
    listing: directories suffixed with ``/``, sorted, capped at ``limit``
    entries with a remaining-count note. File names only — no sizes or
    contents, so the note stays small enough for a dispatch payload.
    """
    if not root.exists():
        return None
    try:
        entries = sorted(p.name + ("/" if p.is_dir() else "") for p in root.iterdir())
    except OSError:
        return None
    if not entries:
        return None
    shown = entries[:limit]
    lines = list(shown)
    if len(entries) > limit:
        lines.append(f"... and {len(entries) - limit} more")
    return "\n".join(lines)


async def workspace_diff(workspace: Path) -> str:
    """The git diff of ``workspace`` including untracked files ('' if not a repo)."""
    if not _is_git_repo(workspace):
        return ""
    try:
        await _git("add", "-N", ".", cwd=workspace)
        return await _git("diff", cwd=workspace)
    except (TimeoutError, FileNotFoundError, OSError):
        return ""


# --- filesystem snapshots -------------------------------------------------
# Git is not available (and usually not correct) inside the worker containers:
# the image ships no git, and /workspace often sits INSIDE a larger repo, so a
# git diff there would report the repo's source edits, not the coder's work.
# Harnesses therefore take a plain path -> (size, mtime) snapshot before the
# run and treat any new or touched file as the product of that run.

IGNORED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "ai_models",
}


def snapshot_tree(root: Path, limit: int = 50_000) -> dict[str, tuple[int, int]]:
    """Map every regular file under ``root`` to (size, mtime_ns).

    Directories in IGNORED_DIRS are pruned (model blobs and VCS internals are
    never coder artifacts and would dominate the walk). Capped at ``limit``
    entries so a pathological workspace cannot stall a run.
    """
    snap: dict[str, tuple[int, int]] = {}
    if not root.is_dir():
        return snap
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
        for name in filenames:
            path = Path(dirpath) / name
            try:
                st = path.stat()
            except OSError:
                continue
            snap[str(path.relative_to(root))] = (st.st_size, st.st_mtime_ns)
            if len(snap) >= limit:
                return snap
    return snap


def compare_trees(
    root: Path, before: dict[str, tuple[int, int]], max_bytes: int = 1_000_000
) -> dict[str, str]:
    """Read every file that is new or changed relative to ``before``.

    Binary files (NUL byte in the first 8 KiB) and files above ``max_bytes``
    are skipped: FileArtifact carries text. Deletions cannot be expressed and
    are ignored.
    """
    after = snapshot_tree(root)
    changed: dict[str, str] = {}
    for rel, (size, mtime) in after.items():
        if before.get(rel) == (size, mtime):
            continue
        path = root / rel
        try:
            data = path.read_bytes()[: max_bytes + 1]
        except OSError:
            continue
        if len(data) > max_bytes or b"\x00" in data[:8192]:
            continue
        changed[rel] = data.decode(errors="replace")
    return changed
