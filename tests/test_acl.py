"""M9: pure gate logic — sender identity, capability gate, pinned inbox."""

from __future__ import annotations

import time

import pytest

from memnc import acl
from memnc.schema import GrantLevel, GrantRecord


def _grant(agent_id="a1", level=GrantLevel.CHAIN, ttl_s=0, granted_by="orchestrator"):
    return GrantRecord(
        agent_id=agent_id,
        level=level,
        granted_by=granted_by,
        expires_at=(time.time() + ttl_s) if ttl_s != 0 else 0,
    )


class TestRegistryWrite:
    def test_no_grant_is_denied(self):
        d = acl.check_registry_write(None, "a1")
        assert not d
        assert "deny-by-default" in d.reason

    def test_chain_grant_allows_own_write(self):
        assert acl.check_registry_write(_grant(), "a1")

    def test_root_grant_allows_own_write(self):
        assert acl.check_registry_write(_grant(level=GrantLevel.ROOT), "a1")

    def test_auto_grant_is_denied(self):
        d = acl.check_registry_write(_grant(level=GrantLevel.AUTO), "a1")
        assert not d
        assert "AUTO" in d.reason

    def test_expired_grant_is_denied(self):
        d = acl.check_registry_write(_grant(ttl_s=-10), "a1", now=time.time())
        assert not d
        assert "expired" in d.reason

    def test_grant_for_other_agent_is_denied(self):
        d = acl.check_registry_write(_grant(agent_id="other"), "a1")
        assert not d
        assert "impersonation" in d.reason


class TestGrantIssuance:
    def test_root_may_issue(self):
        assert acl.check_grant_issuance(_grant(level=GrantLevel.ROOT))

    def test_chain_may_not_issue(self):
        assert not acl.check_grant_issuance(_grant(level=GrantLevel.CHAIN))

    def test_no_grant_may_not_issue(self):
        assert not acl.check_grant_issuance(None)

    def test_expired_root_may_not_issue(self):
        root = GrantRecord(
            agent_id="root", level=GrantLevel.ROOT, expires_at=time.time() - 1
        )
        assert not acl.check_grant_issuance(root, now=time.time())


class TestSenderSubject:
    def test_matching_trailing_token_passes(self):
        assert acl.check_sender("mesh.dm.inst.planner.a1", "a1")

    def test_mismatched_trailing_token_denied(self):
        d = acl.check_sender("mesh.dm.inst.planner.a1", "someone-else")
        assert not d
        assert "impersonation" not in d.reason  # reason names the mismatch
        assert "claims sender" in d.reason

    def test_subject_without_sender_token_passes(self):
        # plain §5.2 subjects are policed at the NATS ACL level, not here
        assert acl.check_sender("mesh.task.submit", "a1")

    def test_empty_sender_denied(self):
        assert not acl.check_sender("mesh.dm.inst.planner.a1", "")


class TestPinnedInbox:
    def test_pinned_inbox_shape(self):
        assert acl.pinned_inbox("a1") == "mesh.agent.a1.work"

    def test_dotted_agent_id_rejected(self):
        with pytest.raises(ValueError):
            acl.pinned_inbox("bad.id")
