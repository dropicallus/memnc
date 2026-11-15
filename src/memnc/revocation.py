"""M7: revocation tombstones — hard cut for a withdrawn/untrusted node.

The grant ledger and the claim gate already control what an agent may do
while it cooperates. Revocation covers the case where it must be cut
entirely: a tombstone record in the ``mesh_revocations`` KV bucket means
the node is excluded from roster discovery, its capability records are
withdrawn, and every future task claim from it is denied — while its
in-flight tasks are allowed to finish (revocation is about *future* work,
never about pulling work back).

Revocation is always a human decision (``meshctl revoke-node``); nothing
in the mesh auto-revokes. Reinstate deletes the tombstone; the node
rejoins on its next heartbeat.
"""

from __future__ import annotations

from .schema import BaseModel

REVOCATIONS_BUCKET = "mesh_revocations"
REVOCATION_KEY = "revoked.{node_id}"


def revocation_key(node_id: str) -> str:
    """KV key for one node's tombstone (KV-illegal chars transliterated)."""
    safe = "".join(c if c.isalnum() or c in "-_./" else "-" for c in node_id)
    return REVOCATION_KEY.format(node_id=safe)


def node_from_key(key: str) -> str | None:
    """Inverse of :func:`revocation_key`; ``None`` for foreign keys."""
    prefix = "revoked."
    if not key.startswith(prefix):
        return None
    return key[len(prefix):]


class RevocationRecord(BaseModel):
    """One revoked node: who cut it, why, and when (epoch seconds)."""

    node_id: str
    reason: str = ""
    by: str = ""
    revoked_at: float = 0.0

    def key(self) -> str:
        return revocation_key(self.node_id)
