"""Workspace-state helpers shared by harnesses and their callers.

The harness contract is workspace state: after ``run`` the git diff of the
workspace IS the change. These helpers turn that state into file artifacts
for the ResultPayload contract.
"""

from __future__ import annotations

import asyncio
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
