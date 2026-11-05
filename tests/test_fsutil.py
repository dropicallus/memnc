import os
from pathlib import Path

import pytest

from memnc.fsutil import (
    PathEscapeError,
    apply_artifacts,
    list_tree,
    read_text_limited,
    safe_path,
)


def test_safe_path_rejects_parent_escape(tmp_path):
    with pytest.raises(PathEscapeError):
        safe_path(tmp_path, "../outside.txt")


def test_safe_path_rejects_absolute_outside(tmp_path):
    with pytest.raises(PathEscapeError):
        safe_path(tmp_path, str(tmp_path.parent / "outside.txt"))


def test_safe_path_rejects_symlink_escape(tmp_path):
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("secret")
    link = tmp_path / "link"
    try:
        os.symlink(outside, link)
    except OSError:  # pragma: no cover - platform without symlinks
        pytest.skip("symlinks unavailable")
    with pytest.raises(PathEscapeError):
        safe_path(tmp_path, "link")


def test_safe_path_allows_nested(tmp_path):
    assert safe_path(tmp_path, "a/b/c.txt") == (tmp_path / "a/b/c.txt").resolve()


def test_safe_path_strips_redundant_workspace_prefix(tmp_path):
    # Task text says "write workspace/m14_claim.py" while the tool root is
    # already .../workspace — the redundant prefix must not nest the file.
    root = tmp_path / "workspace"
    root.mkdir()
    assert safe_path(root, "workspace/m14_claim.py") == (root / "m14_claim.py").resolve()
    assert safe_path(root, "workspace/workspace/x.py") == (root / "x.py").resolve()
    # A genuine subdirectory with a different name is untouched.
    assert safe_path(root, "src/a.py") == (root / "src/a.py").resolve()


def test_read_text_limited(tmp_path):
    target = tmp_path / "big.txt"
    target.write_text("abcdef")
    content, truncated = read_text_limited(target, 3)
    assert content == "abc"
    assert truncated is True


def test_apply_artifacts_writes_files(tmp_path):
    written = apply_artifacts(
        tmp_path,
        [
            {"path": "src/a.py", "content": "x = 1\n"},
            {"path": "notes.md", "content": "# notes\n"},
        ],
    )
    assert sorted(written) == ["notes.md", "src/a.py"]
    assert (tmp_path / "src/a.py").read_text() == "x = 1\n"


def test_apply_artifacts_skips_oversized(tmp_path):
    written = apply_artifacts(
        tmp_path, [{"path": "big.txt", "content": "x" * 50}], max_bytes=10
    )
    assert written == []
    assert not (tmp_path / "big.txt").exists()


def test_apply_artifacts_rejects_escape(tmp_path):
    with pytest.raises(PathEscapeError):
        apply_artifacts(tmp_path, [{"path": "../evil.txt", "content": "bad"}])


def test_list_tree_respects_depth_and_ignores_git(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x")
    (tmp_path / "src" / "deep").mkdir()
    (tmp_path / "src" / "deep" / "b.py").write_text("y")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("no")
    entries = list_tree(tmp_path, depth=2)
    assert "src/a.py" in entries
    assert "src/deep/b.py" not in entries
    assert all(not entry.startswith(".git") for entry in entries)
