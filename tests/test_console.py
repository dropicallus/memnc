"""Tests for the mesh console (S3)."""

import time

from memnc import console
from memnc.schema import GrantLevel, GrantRecord


class TestFormatGrants:
    def _grant(self, expires_at: float) -> GrantRecord:
        return GrantRecord(
            agent_id="node-b-agent",
            level=GrantLevel.ROOT,
            granted_by="root",
            expires_at=expires_at,
        )

    def test_never_expires(self):
        rows = console._format_grants([self._grant(0)])
        assert rows == [
            {
                "agent_id": "node-b-agent",
                "level": "root",
                "granted_by": "root",
                "expires": "never",
            }
        ]

    def test_expiry_seconds_remaining(self):
        rows = console._format_grants([self._grant(time.time() + 120)])
        assert rows[0]["expires"].startswith("in ")
