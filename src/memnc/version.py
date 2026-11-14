"""Node code version: a deterministic fingerprint of the installed memnc tree.

Nodes self-update to the mesh code version before joining (P4 slice 2).
Every worker stamps its code fingerprint into the roster (NodeInfo.version)
and the orchestrator stamps its own fingerprint into every dispatch command;
a worker running different code refuses the command when enforcement is on.

The fingerprint is a short hash over the package's .py file contents
(relative path + bytes), so any source change moves it without a
version bump. Layout:

    <package version>+<hash12>      e.g. "0.1.0+3f9c2a1b7d44"
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from . import __version__

_HASH_LEN = 12
_SEPARATOR = "+"

_cache: str | None = None


def compute_version(root: Path) -> str:
    """Fingerprint the package source tree at ``root`` (pure, uncached)."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        digest.update(rel.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return f"{__version__}{_SEPARATOR}{digest.hexdigest()[:_HASH_LEN]}"


def code_version() -> str:
    """Cached fingerprint of THIS process's installed memnc package."""
    global _cache
    if _cache is None:
        _cache = compute_version(Path(__file__).resolve().parent)
    return _cache


def _code_hash(version: str) -> str:
    """The hash component of a version string ("" when absent)."""
    if _SEPARATOR not in version:
        return ""
    return version.rsplit(_SEPARATOR, 1)[1]


def is_stale(node_version: str, mesh_version: str) -> bool:
    """True when a node runs different code than the mesh orchestrator.

    Fail-open: versions without a hash component ("dev" builds, missing
    fields) are treated as UNKNOWN, not stale — refusing them would brick
    mixed fleets where one node has not been upgraded to fingerprinting yet.
    Enforcement is opt-in anyway (Settings.enforce_node_version).
    """
    node_hash = _code_hash(node_version)
    mesh_hash = _code_hash(mesh_version)
    if not node_hash or not mesh_hash:
        return False
    return node_hash != mesh_hash
