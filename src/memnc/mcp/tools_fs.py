"""Filesystem tool implementations (plain functions, easy to unit-test)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..fsutil import atomic_write, list_tree, read_text_limited, safe_path


def _ok(**kwargs: Any) -> str:
    return json.dumps({"ok": True, **kwargs})


def _error(exc: Exception) -> str:
    return json.dumps({"ok": False, "error": str(exc)})


def read_file(root: Path, path: str, max_bytes: int = 200_000) -> str:
    try:
        target = safe_path(root, path)
        content, truncated = read_text_limited(target, max_bytes)
        return _ok(path=path, content=content, truncated=truncated)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


def write_file(root: Path, path: str, content: str) -> str:
    try:
        target = safe_path(root, path)
        atomic_write(target, content)
        return _ok(path=path, bytes=len(content.encode("utf-8")))
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


def list_dir(root: Path, path: str = ".", depth: int = 2) -> str:
    try:
        base = safe_path(root, path)
        root = Path(root).resolve()
        if base.is_file():
            return _ok(path=path, files=[path])
        entries = list_tree(root, depth=max(1, min(depth, 6)))
        prefix = "" if path in (".", "") else str(Path(path).as_posix()).strip("/") + "/"
        if prefix:
            entries = [entry for entry in entries if entry.startswith(prefix)]
        return _ok(path=path, files=entries[:300])
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


def search_text(
    root: Path,
    pattern: str,
    glob: str = "**/*",
    max_results: int = 100,
) -> str:
    try:
        root = Path(root).resolve()
        regex = re.compile(pattern)
        matches: list[dict[str, Any]] = []
        scanned = 0
        glob_path = Path(glob)
        if glob_path.is_absolute() or ".." in glob_path.parts:
            raise ValueError("glob must be relative and stay inside the workspace")
        for file_path in root.glob(glob):
            if not file_path.is_file() or file_path.is_symlink():
                continue
            if ".git" in file_path.parts or "__pycache__" in file_path.parts:
                continue
            if not file_path.resolve().is_relative_to(root):
                continue  # e.g. a symlinked parent escaped the root
            scanned += 1
            if scanned > 5000:
                break
            try:
                text = file_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    matches.append(
                        {
                            "file": str(file_path.relative_to(root)),
                            "line": number,
                            "text": line[:400],
                        }
                    )
                    if len(matches) >= max_results:
                        return _ok(matches=matches, truncated=True)
        return _ok(matches=matches, truncated=False)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)
