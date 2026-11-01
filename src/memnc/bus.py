"""NATS transport: JetStream stream, KV snapshots, pulls and dedupe."""

from __future__ import annotations

import asyncio
import logging

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
from nats.js.errors import NotFoundError

from .config import Settings
from .schema import (
    Envelope,
    Role,
    SUBJECT_DLQ,
    SUBJECT_EVENTS,
    SUBJECT_RESULT,
    TaskSnapshot,
)

log = logging.getLogger(__name__)


class Bus:
    """Thin wrapper so components never touch raw NATS APIs directly."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.nc: NATSClient | None = None
        self.js: JetStreamContext | None = None
        self.kv = None

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
                subjects=["mesh.>"],
                retention=RetentionPolicy.LIMITS,
                max_age=7 * 24 * 3600 * 1_000_000_000,
                max_msgs=1_000_000,
                duplicate_window=120 * 1_000_000_000,
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
        except Exception:  # noqa: BLE001 - older/newer client API shapes
            keys = [key async for key in self.kv.keys()]
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
