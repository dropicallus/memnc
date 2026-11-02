"""NATS transport: JetStream stream, KV snapshots, pulls and dedupe."""

from __future__ import annotations

import asyncio
import logging
import time

import nats
from nats.aio.client import Client as NATSClient
from nats.aio.msg import Msg
from nats.js import JetStreamContext
from nats.js.api import (
    AckPolicy,
    ConsumerConfig,
    DeliverPolicy,
    KeyValueConfig,
    RetentionPolicy,
    StreamConfig,
)
from nats.js.errors import NotFoundError, NoKeysError

from . import acl
from .config import Settings
from .schema import (
    CAPABILITY_KEY,
    GRANT_BUCKET,
    Capability,
    GrantLevel,
    GrantRecord,
    Envelope,
    REGISTRY_BUCKET,
    Role,
    SUBJECT_DLQ,
    SUBJECT_EVENTS,
    SUBJECT_RESULT,
    TRUST_ACCRUE,
    TRUST_BUCKET,
    TRUST_DECAY,
    TRUST_DEFAULT,
    TRUST_MAX,
    TRUST_MIN,
    TaskSnapshot,
    TrustEntry,
)

# How long a capability record lives without a refresh. Short TTL = presence:
# dead/hung agents fall out of the registry on their own (plan §0.5, D5).
REGISTRY_TTL = 120  # seconds

log = logging.getLogger(__name__)


class Bus:
    """Thin wrapper so components never touch raw NATS APIs directly."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.nc: NATSClient | None = None
        self.js: JetStreamContext | None = None
        self.kv = None
        self._registry = None
        self._trust = None
        self._grants = None

    # -- lifecycle ---------------------------------------------------------
    async def connect(self, attempts: int = 20) -> None:
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                self.nc = await nats.connect(
                    self.settings.nats_url,
                    token=self.settings.nats_token or None,
                    name=self.settings.nats_name or self.settings.node_name,
                    max_reconnect_attempts=-1,
                    reconnect_time_wait=1,
                )
                break
            except Exception as exc:  # noqa: BLE001 - retry any connect failure
                last_error = exc
                log.warning(
                    "nats connect failed (%s/%s): %s", attempt, attempts, exc
                )
                await asyncio.sleep(min(attempt, 5))
        else:
            raise RuntimeError(f"could not connect to NATS: {last_error}")

        self.js = self.nc.jetstream()
        await self.ensure_stream()
        await self.ensure_kv()
        await self.ensure_registry()
        await self.ensure_trust()
        await self.ensure_grants()
        log.info("connected to NATS at %s", self.settings.nats_url)

    async def close(self) -> None:
        if self.nc is not None and not self.nc.is_closed:
            await self.nc.drain()

    async def ensure_stream(self) -> None:
        assert self.js is not None
        try:
            await self.js.stream_info(self.settings.stream_name)
            return
        except NotFoundError:
            pass
        await self.js.add_stream(
            StreamConfig(
                name=self.settings.stream_name,
                # NOTE: `mesh.task.submit` is intentionally NOT covered here.
                # It is a synchronous request/reply channel; if it were part of
                # the JetStream stream, NATS would answer `nc.request()` with the
                # JS publish-ack ({stream, seq}) instead of the handler's
                # `message.respond()` result. Everything that actually flows to
                # the stream (commands, results, auction/bids, task events, dlq)
                # stays covered.
                subjects=[
                    "mesh.agent.>",
                    "mesh.auction.>",
                    "mesh.bid.>",
                    "mesh.task.*.events",
                    "mesh.dlq",
                    "mesh.node.>",
                ],
                retention=RetentionPolicy.LIMITS,
                # nats-py 2.x: max_age / duplicate_window are SECONDS;
                # the py lib converts to nanoseconds for the server.
                max_age=7 * 24 * 3600,
                max_msgs=1_000_000,
                duplicate_window=120,
                storage="file",
            )
        )
        log.info("created stream %s", self.settings.stream_name)

    async def ensure_kv(self) -> None:
        assert self.js is not None
        try:
            self.kv = await self.js.key_value(self.settings.kv_bucket)
        except NotFoundError:
            self.kv = await self.js.create_key_value(
                KeyValueConfig(bucket=self.settings.kv_bucket, history=10)
            )
            log.info("created KV bucket %s", self.settings.kv_bucket)

    # -- M7: capability registry -------------------------------------------
    async def ensure_registry(self) -> None:
        """Ensure the TTL capability-registry KV bucket exists."""
        assert self.js is not None
        try:
            self._registry = await self.js.key_value(REGISTRY_BUCKET)
        except NotFoundError:
            self._registry = await self.js.create_key_value(
                KeyValueConfig(
                    bucket=REGISTRY_BUCKET,
                    history=1,
                    # TTL = presence: expired capability records vanish.
                    ttl=REGISTRY_TTL,
                )
            )
            log.info("created registry KV bucket %s (ttl=%ss)", REGISTRY_BUCKET, REGISTRY_TTL)

    async def advertise_capability(self, cap: Capability) -> None:
        """Write (or refresh) one capability record into the registry KV.

        With ``MESH_GATE=1`` the write is authorized against the grant ledger
        (deny-by-default): only ROOT/CHAIN-granted agents pass, AUTO and
        grantless writes raise ``PermissionError``.
        """
        assert self._registry is not None
        if self.settings.mesh_gate:
            decision = acl.check_registry_write(
                await self.grant_read(cap.agent_id), cap.agent_id
            )
            if not decision:
                raise PermissionError(f"registry write denied for {cap.agent_id}: {decision.reason}")
        await self._registry.put(cap.key(), cap.model_dump_json().encode())
        log.debug("advertised capability %s", cap.key())

    async def withdraw_capability(self, agent_id: str, skill: str | None = None) -> None:
        """Delete one capability record (or all skills for an agent)."""
        assert self._registry is not None
        if skill is not None:
            await self._registry.delete(Capability(agent_id=agent_id, skill=skill).key())
            return
        for cap in await self.list_capabilities():
            if cap.agent_id == agent_id:
                try:
                    await self._registry.delete(cap.key())
                except (KeyError, NotFoundError):  # already gone
                    pass

    async def list_capabilities(self) -> list[Capability]:
        """Return all live capability records, sorted by key (agent, skill)."""
        assert self._registry is not None
        out: list[Capability] = []
        try:
            keys = await self._registry.keys()
        except NoKeysError:
            return out
        for key in sorted(keys):
            entry = await self._registry.get(key)
            out.append(Capability.model_validate_json(entry.value))
        return out

    # -- M9: capability grants (permission chain) --------------------------
    async def ensure_grants(self) -> None:
        """Ensure the grant-ledger KV bucket exists."""
        assert self.js is not None
        try:
            self._grants = await self.js.key_value(GRANT_BUCKET)
        except NotFoundError:
            self._grants = await self.js.create_key_value(
                KeyValueConfig(bucket=GRANT_BUCKET, history=10)
            )
            log.info("created grant KV bucket %s", GRANT_BUCKET)

    async def issue_grant(
        self,
        agent_id: str,
        level: GrantLevel = GrantLevel.CHAIN,
        granted_by: str = "orchestrator",
        ttl_s: float = 0,
    ) -> GrantRecord:
        """Mint and store a grant for ``agent_id`` (ROOT only does this)."""
        assert self._grants is not None
        grant = GrantRecord(
            agent_id=agent_id,
            level=level,
            granted_by=granted_by,
            expires_at=(time.time() + ttl_s) if ttl_s > 0 else 0,
        )
        await self._grants.put(grant.key(), grant.model_dump_json().encode())
        log.info("issued %s grant to %s (by %s)", level.value, agent_id, granted_by)
        return grant

    async def revoke_grant(self, agent_id: str) -> None:
        assert self._grants is not None
        await self._grants.delete(GrantRecord(agent_id=agent_id).key())

    async def grant_read(self, agent_id: str) -> GrantRecord | None:
        assert self._grants is not None
        try:
            entry = await self._grants.get(GrantRecord(agent_id=agent_id).key())
        except (KeyError, NotFoundError):
            return None
        return GrantRecord.model_validate_json(entry.value)

    async def list_grants(self) -> list[GrantRecord]:
        assert self._grants is not None
        out: list[GrantRecord] = []
        try:
            keys = await self._grants.keys()
        except NoKeysError:
            return out
        for key in sorted(keys):
            entry = await self._grants.get(key)
            out.append(GrantRecord.model_validate_json(entry.value))
        return out

    # -- M8: trust ledger --------------------------------------------------
    async def ensure_trust(self) -> None:
        """Ensure the trust-ledger KV bucket exists."""
        assert self.js is not None
        try:
            self._trust = await self.js.key_value(TRUST_BUCKET)
        except NotFoundError:
            self._trust = await self.js.create_key_value(
                KeyValueConfig(bucket=TRUST_BUCKET, history=1)
            )
            log.info("created trust KV bucket %s", TRUST_BUCKET)

    async def trust_entry(self, subject: str) -> TrustEntry | None:
        """Return the current trust entry for an agent, or None if absent."""
        assert self._trust is not None
        try:
            entry = await self._trust.get(f"trust.{subject}")
        except (KeyError, NotFoundError):
            return None
        return TrustEntry.model_validate_json(entry.value)

    async def trust_credit(
        self, subject: str, rating: str, attribution: list[str] | None = None
    ) -> TrustEntry:
        """Accrue TRUST_ACCRUE credit to an agent, clamped to [MIN, MAX]."""
        assert self._trust is not None
        current = await self.trust_entry(subject)
        credit = (current.credit if current else TRUST_DEFAULT) + TRUST_ACCRUE
        credit = max(TRUST_MIN, min(TRUST_MAX, credit))
        entry = TrustEntry(
            subject=subject,
            rating=rating,
            credit=credit,
            attribution=list(attribution or []),
        )
        await self._trust.put(f"trust.{subject}", entry.model_dump_json().encode())
        return entry

    async def trust_decay(self, subject: str) -> TrustEntry:
        """Multiply an agent's credit by TRUST_DECAY, clamped to [MIN, MAX]."""
        assert self._trust is not None
        current = await self.trust_entry(subject)
        credit = (current.credit if current else TRUST_DEFAULT) * TRUST_DECAY
        credit = max(TRUST_MIN, min(TRUST_MAX, credit))
        entry = TrustEntry(
            subject=subject,
            rating=current.rating if current else "decay",
            credit=credit,
            attribution=list(current.attribution if current else []),
        )
        await self._trust.put(f"trust.{subject}", entry.model_dump_json().encode())
        return entry

    async def list_trust(self) -> list[TrustEntry]:
        """Return all trust entries, sorted by subject."""
        assert self._trust is not None
        out: list[TrustEntry] = []
        try:
            keys = await self._trust.keys()
        except NoKeysError:
            return out
        for key in sorted(keys):
            if not key.startswith("trust."):
                continue
            entry = await self._trust.get(key)
            out.append(TrustEntry.model_validate_json(entry.value))
        return out


    # -- publish -----------------------------------------------------------
    async def publish_envelope(self, subject: str, envelope: Envelope) -> None:
        assert self.js is not None
        await self.js.publish(
            subject,
            envelope.model_dump_json().encode(),
            headers={"Nats-Msg-Id": envelope.id},
        )

    async def emit_event(self, snapshot: TaskSnapshot, event: str, detail: str = "") -> None:
        envelope = Envelope(
            task_id=snapshot.task_id,
            trace_id=snapshot.trace_id,
            kind="event",
            node=self.settings.node_name,
            state=snapshot.state,
            attempt=snapshot.attempt,
            payload={
                "event": event,
                "detail": detail,
                "state": snapshot.state.value,
                "revisions": snapshot.revisions,
            },
        )
        await self.publish_envelope(
            SUBJECT_EVENTS.format(task_id=snapshot.task_id), envelope
        )

    async def dead_letter(self, envelope: Envelope, reason: str) -> None:
        envelope.payload = {**envelope.payload, "dlq_reason": reason}
        await self.publish_envelope(SUBJECT_DLQ, envelope)

    # -- snapshots ---------------------------------------------------------
    async def get_snapshot(self, task_id: str) -> TaskSnapshot | None:
        assert self.kv is not None
        try:
            entry = await self.kv.get(f"task.{task_id}")
        except (KeyError, NotFoundError):
            return None
        return TaskSnapshot.model_validate_json(entry.value)

    async def put_snapshot(self, snapshot: TaskSnapshot) -> None:
        assert self.kv is not None
        await self.kv.put(
            f"task.{snapshot.task_id}", snapshot.model_dump_json().encode()
        )

    async def list_tasks(self) -> list[TaskSnapshot]:
        assert self.kv is not None
        try:
            keys = await self.kv.keys()
        except nats.js.errors.NoKeysError:
            keys = []
        snapshots = []
        for key in keys:
            if not key.startswith("task."):
                continue
            snapshot = await self.get_snapshot(key.removeprefix("task."))
            if snapshot is not None:
                snapshots.append(snapshot)
        return snapshots

    # -- consumers ---------------------------------------------------------
    async def pull_commands(
        self, role: Role, ack_wait_s: float | None = None, max_deliver: int = 3
    ):
        assert self.js is not None
        durable = f"worker-{role.value}"
        return await self._pull(
            SUBJECT_CMD_FOR(role),
            durable,
            ack_wait_s=ack_wait_s or self.settings.timeout_for(role) * 1.5,
            max_deliver=max_deliver,
        )

    async def pull_results(self):
        assert self.js is not None
        return await self._pull(
            SUBJECT_RESULT.format(role="*"),
            "orchestrator-results",
            ack_wait_s=60,
            max_deliver=5,
        )

    async def _pull(
        self,
        subject: str,
        durable: str,
        *,
        ack_wait_s: float,
        max_deliver: int,
    ):
        assert self.js is not None
        config = ConsumerConfig(
            ack_policy=AckPolicy.EXPLICIT,
            ack_wait=ack_wait_s,
            max_deliver=max_deliver,
            deliver_policy=DeliverPolicy.ALL,
        )
        try:
            return await self.js.pull_subscribe(
                subject,
                durable=durable,
                stream=self.settings.stream_name,
                config=config,
            )
        except Exception as exc:  # noqa: BLE001 - existing consumer config mismatch
            log.debug("consumer %s already exists (%s); reusing", durable, exc)
            return await self.js.pull_subscribe(
                subject, durable=durable, stream=self.settings.stream_name
            )

    async def fetch(self, subscription, batch: int = 1, timeout_s: float = 5.0) -> list[Msg]:
        try:
            return await subscription.fetch(batch, timeout=timeout_s)
        except Exception as exc:  # noqa: BLE001 - nats TimeoutError or fetch timeout
            if type(exc).__name__ in {"TimeoutError", "FetchTimeoutError"}:
                return []
            raise


def SUBJECT_CMD_FOR(role: Role) -> str:
    from .schema import SUBJECT_CMD

    return SUBJECT_CMD.format(role=role.value)
