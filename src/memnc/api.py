"""M11a Mesh API — anycast read path over NATS.

Concept: docs/mesh-api-concept.md §4/§5. The server answers
``mesh.api.request.<verb>`` requests (queue-group ``mesh-api`` — anycast,
exactly one server answers) and replies with ``served_by`` so frontends
always know which node answered.

Requests/replies are plain JSON (not Envelope) for v1: the Envelope schema
carries task framing that read queries don't need. Additive deviation from
the concept doc, recorded here.

The server exposes, it does not re-implement: all state comes from the
existing Bus (KV roster, task snapshots).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .bus import Bus
from .config import Settings

log = logging.getLogger(__name__)

API_SUBJECT = "mesh.api.request.{verb}"
API_QUEUE = "mesh-api"

TERMINAL_STATES = {"DONE", "FAILED", "CANCELLED"}


class ApiServer:
    """Anycast read server. Hosted next to the orchestrator (owns the state)."""

    def __init__(self, settings: Settings, bus: Bus):
        self.settings = settings
        self.bus = bus
        self._sub = None

    async def start(self) -> None:
        assert self.bus.nc is not None
        self._sub = await self.bus.nc.subscribe(
            "mesh.api.request.*",
            queue=API_QUEUE,
            cb=self._on_request,
        )
        log.info("mesh api server listening on mesh.api.request.* (q=%s)", API_QUEUE)

    async def stop(self) -> None:
        if self._sub is not None:
            await self._sub.unsubscribe()

    # -- request handling ---------------------------------------------------

    async def _on_request(self, msg: Any) -> None:
        """Anycast entry point. Always replies, even on error."""
        try:
            req: dict[str, Any] = json.loads(msg.data)
            verb = req.get("verb", "")
            args = req.get("args", {}) or {}
            data = await self.dispatch(verb, args)
            reply = {"ok": True, "data": data}
        except Exception as exc:  # noqa: BLE001 — API must never drop a reply
            req = {}
            reply = {"ok": False, "error": str(exc)}
        reply["served_by"] = self.settings.node_name
        reply["request_id"] = req.get("request_id")
        if msg.reply and self.bus.nc is not None:
            await self.bus.nc.publish(
                msg.reply, json.dumps(reply).encode()
            )

    async def dispatch(self, verb: str, args: dict[str, Any]) -> Any:
        if verb == "task.list":
            return await self.task_list(
                state=args.get("state"), node=args.get("node")
            )
        if verb == "task.detail":
            tid = args.get("task_id", "")
            snap = await self.bus.get_snapshot(tid)
            if snap is None:
                raise ValueError(f"unknown task {tid}")
            return snap.model_dump()
        if verb == "node.list":
            return [n.model_dump() for n in await self.bus.list_nodes()]
        if verb == "node.detail":
            nid = args.get("node_id", "")
            node = await self.bus.get_node(nid)
            if node is None:
                raise ValueError(f"unknown node {nid}")
            tasks = await self.task_list(node=nid)
            return {"node": node.model_dump(), "tasks": tasks}
        raise ValueError(f"unknown verb {verb!r}")

    async def task_list(
        self, state: str | None = None, node: str | None = None
    ) -> list[dict[str, Any]]:
        """Task summaries, filtered. state and node are prefix-match on the
        snapshot's state / claimed_by."""
        out: list[dict[str, Any]] = []
        for snap in await self.bus.list_tasks():
            if state and not snap.state.value.upper().startswith(
                state.upper()
            ):
                continue
            if node and not (snap.claimed_by or "").startswith(node):
                continue
            out.append(snap.model_dump())
        return out


async def api_request(
    bus: Bus, verb: str, *, timeout: float = 10.0, **args: Any
) -> dict[str, Any]:
    """Client side: send one API request, return the full reply.

    Raises RuntimeError on error replies so frontends can surface the cause.
    """
    assert bus.nc is not None
    req = {"verb": verb, "args": args}
    reply = await bus.nc.request(
        API_SUBJECT.format(verb=verb.split(".")[0]),
        json.dumps(req).encode(),
        timeout=timeout,
    )
    resp = json.loads(reply.data)
    if not resp.get("ok"):
        raise RuntimeError(f"api {verb}: {resp.get('error', 'error')}")
    return resp
