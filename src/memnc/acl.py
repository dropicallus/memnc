"""M9: subject-carried identity, ACL gate and capability grants (pure logic).

Three ormica/Synadia-derived rules, implemented as NATS-free pure functions so
they are unit-testable without a broker (plan §0.5 M9):

* **Sender identity rides the subject** (Cotal/D4): a sender-carried subject is
  ``mesh.<space>.<mode>.<target>.<sender>``; the trailing token *is* the
  claimed identity.  A message claiming identity X on a subject ending in
  ``.Y`` is an impersonation attempt -> deny.
* **Capability gate (ormica permission chain)**: a node may only write to the
  capability registry if it holds a grant issued by the orchestrator (ROOT) or
  a ROOT-granted issuer (CHAIN).  Self-declared authority (AUTO) and *no
  grant* are denied — deny-by-default.
* **Pinned per-agent durable inbox**: the only work subject an agent may
  answer is ``mem.agent.<agent_id>.work`` (plus the anycast ``mem.agent.work``
  for anonymous pickup).  Nobody else's inbox is addressable for it.

Gate decisions are :class:`GateDecision` objects (allowed + machine-readable
reason) — never bare booleans, so denials are loggable/auditable.
"""

from __future__ import annotations

import fnmatch

import time
from dataclasses import dataclass
from enum import Enum

from .schema import GrantLevel, GrantRecord

# Subject an agent may subscribe for direct (pinned) work delivery. The
# anycast subject stays open for anonymous pickup (plan §5.2).
PINNED_INBOX = "mem.agent.{agent_id}.work"
ANYPAST_INBOX = "mem.agent.work"


@dataclass(frozen=True)
class GateDecision:
    """Result of a gate check — loggable, auditable, deny-by-default."""

    allowed: bool
    reason: str

    def __bool__(self) -> bool:  # convenience: `if not decision:`
        return self.allowed


ALLOW = GateDecision(allowed=True, reason="granted")


def check_registry_write(
    grant: GrantRecord | None,
    agent_id: str,
    *,
    now: float | None = None,
) -> GateDecision:
    """Can ``agent_id`` write/refresh its records in the registry KV?

    Deny-by-default: no grant at all, an expired grant, or a grant for a
    *different* agent is rejected.  AUTO (self-declared) is never enough —
    that is the ormica permission-chain: authority is *granted*, not claimed.
    """
    if grant is None:
        return GateDecision(False, "no grant on file (deny-by-default)")
    if grant.agent_id != agent_id:
        return GateDecision(
            False,
            f"grant is for {grant.agent_id!r}, not {agent_id!r} (impersonation?)",
        )
    if grant.level == GrantLevel.AUTO:
        return GateDecision(False, "AUTO self-declaration does not authorize registry writes")
    ts = time.time() if now is None else now
    if grant.expires_at > 0 and ts > grant.expires_at:
        return GateDecision(False, f"grant expired at {grant.expires_at:.0f}")
    return GateDecision(True, f"{grant.level.value} grant by {grant.granted_by}")


def check_grant_issuance(
    issuer: GrantRecord | None,
    *,
    now: float | None = None,
) -> GateDecision:
    """May ``issuer`` hand out grants to other agents?

    Only ROOT may issue grants (CHAIN is a leaf — it can write its own records
    but not mint further authority; that keeps the permission chain depth-1,
    matching the orchestrator as single trust anchor).
    """
    if issuer is None:
        return GateDecision(False, "issuer has no grant (deny-by-default)")
    if issuer.level != GrantLevel.ROOT:
        return GateDecision(False, f"{issuer.level.value} cannot issue grants")
    ts = time.time() if now is None else now
    if issuer.expires_at > 0 and ts > issuer.expires_at:
        return GateDecision(False, "issuer grant expired")
    return ALLOW


def check_sender(subject: str, claimed_sender: str) -> GateDecision:
    """Does a sender-carried subject match the claimed identity?

    ``mesh.<space>.<mode>.<target>.<sender>``: the trailing token must equal
    the claimed sender. Subjects that do not carry a sender token (the plain
    mesh subjects of §5.2) are out of this check's scope and pass — wire
    policing for those happens at the NATS ACL level, not in the payload.
    """
    if not claimed_sender:
        return GateDecision(False, "empty claimed sender")
    parts = subject.split(".")
    if len(parts) < 5:
        return GateDecision(True, "subject carries no sender token (ACL-level policing)")
    trailing = parts[-1]
    if trailing == claimed_sender:
        return ALLOW
    return GateDecision(
        False,
        f"subject ends in {trailing!r} but claims sender {claimed_sender!r}",
    )


def matches_any(name: str, patterns: list[str]) -> bool:
    """True if ``name`` matches any glob ``pattern`` (``node-b-*`` style)."""
    return any(fnmatch.fnmatchcase(name, p.strip()) for p in patterns if p.strip())


def auto_grant_agents(payload: dict, patterns: list[str]) -> list[str]:
    """Agent ids a heartbeat authorizes for a CHAIN grant (M9 bootstrap).

    The orchestrator (ROOT) listens to ``mesh.node.*.heartbeat`` and issues
    grants only for nodes matching ``MESH_GRANT_NODES`` patterns — this is how
    a joining machine (node-b) gets write access without a manual
    ``meshctl grant`` on the orchestrator host. Worker heartbeats carry
    ``{role, node}`` (agent id ``<node>-<role>``); adapter heartbeats carry
    ``{agent_id, ...}``. Returns [] when nothing matches.
    """
    if not patterns:
        return []
    node = str(payload.get("node", ""))
    agent_id = str(payload.get("agent_id", ""))
    role = str(payload.get("role", ""))
    out: list[str] = []
    if agent_id and (matches_any(agent_id, patterns) or matches_any(node, patterns)):
        out.append(agent_id)
    elif node and role and matches_any(f"{node}-{role}", patterns):
        out.append(f"{node}-{role}")
    return out


def pinned_inbox(agent_id: str) -> str:
    """The one work subject pinned to ``agent_id`` (per-agent durable inbox)."""
    if "." in agent_id:
        raise ValueError(f"agent_id must not contain '.': {agent_id!r}")
    return PINNED_INBOX.format(agent_id=agent_id)
