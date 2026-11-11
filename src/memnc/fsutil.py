"""Path-confined filesystem helpers shared by MCP tools and the orchestrator."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


class PathEscapeError(ValueError):
    """Raised when a requested path resolves outside the workspace root."""


def safe_path(root: Path, relative: str) -> Path:
    """Resolve *relative* under *root*, refusing anything that escapes it.

    Resolution follows symlinks, so a symlink inside the workspace pointing
    outside is rejected as well.
    """

    root = Path(root).resolve()
    candidate = Path(relative)
    # Small models often redundantly repeat the workspace directory name in
    # the path ("write workspace/m14_claim.py" while root IS /workspace),
    # which nests files under root/workspace/... Strip the redundant prefix
    # so the file lands where the task text means it to.
    root_name = root.name
    while (
        root_name
        and len(candidate.parts) > 1
        and candidate.parts[0] == root_name
    ):
        candidate = Path(*candidate.parts[1:])
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        resolved = (root / candidate).resolve()
    if resolved != root and not resolved.is_relative_to(root):
        raise PathEscapeError(f"path escapes workspace: {relative}")
    return resolved


def read_text_limited(path: Path, max_bytes: int) -> tuple[str, bool]:
    # Bounded incremental read: never loads the whole (possibly huge) file.
    with path.open("rb") as handle:
        data = handle.read(max_bytes + 1)
    truncated = len(data) > max_bytes
    if truncated:
        data = data[:max_bytes]
    return data.decode("utf-8", errors="replace"), truncated


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".memnc-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def apply_artifacts(
    root: Path,
    files: list[dict] | list,
    max_bytes: int = 200_000,
) -> list[str]:
    """Write model-authored file artifacts into the workspace.

    Returns the list of paths written. Oversized artifacts are skipped rather
    than silently corrupted.
    """

    written: list[str] = []
    for item in files:
        path = item["path"] if isinstance(item, dict) else item.path
        content = item["content"] if isinstance(item, dict) else item.content
        if len(content.encode("utf-8")) > max_bytes:
            continue
        target = safe_path(root, path)
        atomic_write(target, content)
        written.append(str(target.relative_to(Path(root).resolve())))
    return written


def list_tree(root: Path, depth: int = 2, max_entries: int = 200) -> list[str]:
    root = Path(root).resolve()
    entries: list[str] = []
    ignored = {".git", "__pycache__", ".venv", "node_modules"}
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        level = len(current.relative_to(root).parts)
        dirnames[:] = sorted(d for d in dirnames if d not in ignored)
        if level >= depth:
            dirnames[:] = []
        if level + 1 <= depth:
            for name in sorted(filenames)[:max_entries]:
                entries.append(str((current / name).relative_to(root)))
                if len(entries) >= max_entries:
                    return entries
    return entries
