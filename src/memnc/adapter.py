"""M7: heterogeneous agent adapter.

Binds an *external* agent (Hermes, pi, Claude Code, a CLI shim, ...) into the
NATS mesh by capability rather than by fixed role.  The adapter

  * advertises one :class:`~memnc.schema.Capability` record per skill into the
    TTL registry KV bucket (presence: records expire if the adapter dies),
  * heartbeats on its node subject so the mesh sees it as live,
  * subscribes for work on ``mesh.agent.work`` and ``mesh.agent.<agent_id>.work``
    and replies synchronously (request/reply) via ``msg.respond``.

The external agent's real behaviour is behind the ``handler`` callable, so a
Hamster CLI can attach to MCP, a shell shim, another agent harness, etc.  All
transport is just NATS — no new dependencies.

.. note::
   ``agent_id`` and ``skill`` must not contain ``.`` — the registry key layout
   ``registry.<agent_id>.<skill>`` (see :func:`Capability.key`) splits on dots,
   so a dotted id/skill would not round-trip through :func:`Capability.from_key`.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import socket
from typing import Any, Awaitable, Callable

from .bus import Bus
from .config import Settings, get_settings
from .schema import (
    SUBJECT_AGENT_INBOX,
    Capability,
    Envelope,
    Kind,
    SUBJECT_AGENT_WORK,
    SUBJECT_HEARTBEAT,
)

log = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_S = 20.0

# A handler receives the decoded work payload (dict) and returns the response
# dict that is sent back to the caller.  Handlers are declared async; the work
# loop also tolerates a plain sync callable for convenience (see _work_loop).
Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def _check_clean(*, agent_id: str, skill: str) -> None:
    """Registry keys are ``registry.<agent_id>.<skill>`` — dots would break it."""
    if "." in agent_id or "." in skill:
        raise ValueError(
            f"agent_id/skill must not contain '.': got {agent_id!r}/{skill!r}"
        )


class AgentAdapter:
    """Register an external agent in the mesh and pump work to its handler."""

    def __init__(
        self,
        *,
        agent_id: str | None = None,
        node: str | None = None,
        model: str = "",
        contact: str = "",
        skills: list[str] | None = None,
        handler: Handler | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.agent_id = agent_id or socket.gethostname()
        self.node = node or getattr(get_settings(), "node_name", socket.gethostname())
        self.model = model
        self.contact = contact or f"mcp://{self.node}:{self.agent_id}"
        self.skills = skills or ["code"]
        self.handler: Handler = handler or self._echo
        self.settings = settings or get_settings()
        self.bus = Bus(self.settings)
        self._stop = asyncio.Event()

    # -- callbacks ---------------------------------------------------------
    async def _echo(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "echo": payload}

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        await self.bus.connect()  # also ensures the registry KV bucket
        for skill in self.skills:
            _check_clean(agent_id=self.agent_id, skill=skill)
            await self.advertise(skill)
        log.info(
            "adapter up",
            extra={
                "agent_id": self.agent_id,
                "node": self.node,
                "skills": self.skills,
                "contact": self.contact,
            },
        )
        await asyncio.gather(
            self._heartbeat_loop(),
            self._work_loop(SUBJECT_AGENT_WORK),
            self._work_loop(SUBJECT_AGENT_INBOX.format(agent_id=self.agent_id)),
        )

    async def stop(self) -> None:
        self._stop.set()

    async def aclose(self) -> None:
        self._stop.set()
        try:
            # Best-effort withdrawal so a clean shutdown leaves no stale record.
            for skill in self.skills:
                await self.bus.withdraw_capability(self.agent_id, skill=skill)
        except Exception:  # noqa: BLE001 - shutdown is best effort
            log.debug("capability withdrawal failed", exc_info=True)
        await self.bus.close()

    # -- registry ----------------------------------------------------------
    async def advertise(self, skill: str) -> None:
        cap = Capability(
            agent_id=self.agent_id,
            skill=skill,
            node=self.node,
            model=self.model,
            contact=self.contact,
            load=0.0,
            capacity=1,
        )
        await self.bus.advertise_capability(cap)
        log.info("advertised %s", cap.key())

    # -- loops -------------------------------------------------------------
    async def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            envelope = Envelope(
                task_id="heartbeat",
                trace_id="heartbeat",
                kind=Kind.HEARTBEAT,
                node=self.node,
                payload={
                    "agent_id": self.agent_id,
                    "skills": self.skills,
                    "model": self.model,
                    "contact": self.contact,
                },
            )
            try:
                assert self.bus.nc is not None
                await self.bus.nc.publish(
                    SUBJECT_HEARTBEAT.format(node=self.node),
                    envelope.model_dump_json().encode(),
                )
            except Exception:  # noqa: BLE001 - heartbeat is best effort
                log.debug("heartbeat failed", exc_info=True)
            await asyncio.sleep(HEARTBEAT_INTERVAL_S)

    async def _work_loop(self, subject: str) -> None:
        """Core request/reply: respond to work pushed on ``subject``."""
        assert self.bus.nc is not None
        sub = await self.bus.nc.subscribe(subject)
        log.debug("subscribed for work on %s", subject)
        async for msg in sub.messages:
            if self._stop.is_set():
                await msg.respond(b'{"ok": false, "error": "adapter stopping"}')
                break
            try:
                payload = msg.json()
                if not isinstance(payload, dict):
                    raise ValueError(f"expected dict payload, got {type(payload).__name__}")
                out = self.handler(payload)
                if asyncio.iscoroutine(out):
                    out = await out
                result = out
            except Exception as exc:  # noqa: BLE001 - never kill the loop
                log.exception("work handling failed on %s", subject)
                result = {"ok": False, "error": f"adapter error: {exc}"}
            try:
                await msg.respond(result)
            except Exception:  # noqa: BLE001
                log.debug("respond failed on %s", subject)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m memnc.adapter", description="Mesh heterogeneous agent adapter."
    )
    parser.add_argument("--name", help="agent_id (default: hostname)")
    parser.add_argument("--node", help="node name (default: hostname)")
    parser.add_argument("--model", default="", help="model the agent runs on")
    parser.add_argument("--contact", default="", help="how to reach the agent (mcp://, cli:, ...)")
    parser.add_argument("--skill", action="append", default=[], help="skill to advertise (repeatable)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    adapter = AgentAdapter(
        agent_id=args.name,
        node=args.node,
        model=args.model,
        contact=args.contact,
        skills=args.skill or ["code"],
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(adapter.start())
    except KeyboardInterrupt:
        pass
    finally:
        loop.run_until_complete(adapter.aclose())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
