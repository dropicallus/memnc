"""T1: read-only Telegram observer (plan §14.6, T1).

Mirrors mesh ledger activity into Telegram and answers two read-only
commands. No intervention, no write path: every message it sends is derived
from NATS events or KV snapshots. Authorization is a static allow-list of
chat ids (TELEGRAM_ALLOW, comma-separated); anything else is ignored.

Design notes:
- Uses the raw Bot API over httpx (long polling) - no extra dependency.
- Events are published per task on core NATS subjects
  (mesh.task.<id>.events), so a wildcard core subscription sees all of them.
- The Bot API calls never raise into the event loop: delivery problems are
  logged and swallowed (an observer must never disturb the mesh).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx
from nats.aio.msg import Msg

from .bus import Bus
from .config import Settings
from .schema import SUBJECT_EVENTS

log = logging.getLogger(__name__)

API = "https://api.telegram.org"

SUBJECT_ALL_EVENTS = SUBJECT_EVENTS.format(task_id="*")


def allowed_chats(allow: str) -> set[str]:
    """Parse TELEGRAM_ALLOW: comma-separated ids, '@' and whitespace stripped."""
    return {
        part.strip().lstrip("@").lower()
        for part in allow.split(",")
        if part.strip()
    }


def format_event(envelope: dict[str, Any]) -> str | None:
    """One compact line per ledger event, or None for uninteresting ones."""
    payload = envelope.get("payload") or {}
    state = payload.get("state") or envelope.get("state") or "?"
    event = payload.get("event", "")
    detail = (payload.get("detail", "") or "").strip()
    rev = payload.get("revisions")
    task = envelope.get("task_id", "?")
    node = envelope.get("node", "?")
    head = f"#{task[:12]} {state}"
    if rev:
        head += f" rev{rev}"
    line = f"{head} [{node}] {event}"
    if detail:
        line += f" - {detail[:300]}"
    return line


def format_task(snapshot_dict: dict[str, Any]) -> str:
    """Short human-readable task summary for `state <id>` and `tasks`."""
    task = snapshot_dict.get("task_id", "?")
    state = snapshot_dict.get("state", "?")
    claimed = snapshot_dict.get("claimed_by") or "unclaimed"
    rev = snapshot_dict.get("revisions", 0)
    attempts = snapshot_dict.get("attempt", 0)
    title = (snapshot_dict.get("task") or snapshot_dict.get("prompt") or "").strip()
    if len(title) > 120:
        title = title[:117] + "..."
    lines = [
        f"#{task} {state} rev{rev} attempt{attempts}",
        f"claimed_by: {claimed}",
    ]
    if title:
        lines.append(f"task: {title}")
    verdict = snapshot_dict.get("critic_verdict") or ""
    if verdict:
        lines.append(f"verdict: {verdict[:300]}")
    return "\n".join(lines)


def summarize_tasks(snapshots: list[dict[str, Any]], filter_state: str = "", limit: int = 15) -> str:
    """Compact /tasks view: state counts + active tasks + recent completions.

    Optional filter_state (e.g. "failed") restricts the detail lines to that
    state; the header always shows the totals.
    """
    if not snapshots:
        return "no tasks in ledger"
    counts: dict[str, int] = {}
    for s in snapshots:
        counts[s.get("state", "?")] = counts.get(s.get("state", "?"), 0) + 1
    total = len(snapshots)
    header = f"{total} tasks: " + " ".join(
        f"{state}={n}" for state, n in sorted(counts.items(), key=lambda kv: -kv[1])
    )
    wanted = filter_state.strip().lower()
    lines = [header]
    if wanted:
        selected = [s for s in snapshots if s.get("state", "").lower() == wanted]
        if not selected:
            return f"{header}\nno tasks with state: {wanted}"
        lines.append(f"showing {wanted} ({len(selected)}):")
    else:
        # active tasks first, then the most recent terminal ones
        active = [s for s in snapshots if s.get("state") not in ("DONE", "FAILED", "CANCELLED")]
        done = sorted(
            (s for s in snapshots if s.get("state") in ("DONE", "FAILED", "CANCELLED")),
            key=lambda s: s.get("task_id", ""),
        )
        selected = active + done[-limit:]
        if not selected:
            lines.append("no tasks to show")
            return "\n".join(lines)
    for s in selected[-40:]:
        tid = str(s.get("task_id", "?"))[:12]
        state = s.get("state", "?")
        claimed = s.get("claimed_by") or ""
        title = (s.get("task") or s.get("prompt") or "").strip().splitlines()[0][:60]
        line = f"#{tid} {state}"
        if claimed:
            line += f" ({claimed})"
        if title:
            line += f" - {title}"
        lines.append(line)
    if wanted and len(selected) > 40:
        lines.append(f"... and {len(selected) - 40} more")
    return "\n".join(lines)


def format_roster(nodes: list[dict[str, Any]]) -> str:
    """Compact /roster view: one line per live node entry."""
    if not nodes:
        return "no nodes in roster"
    lines = [f"{len(nodes)} nodes:"]
    for n in nodes:
        roles = ",".join(n.get("roles") or []) or "-"
        models = ",".join(n.get("models") or []) or "-"
        vram = n.get("vram_gb") or 0
        gpu = n.get("gpu") or "-"
        lines.append(
            f"{n.get('node_id', '?')} roles={roles} models={models} "
            f"vram={vram:g}GB gpu={gpu}"
        )
    return "\n".join(lines)


class TelegramObserver:
    """Read-only mirror: NATS events -> Telegram, Telegram commands -> KV."""

    def __init__(self, settings: Settings, bot: "BotAPI"):
        self.settings = settings
        self.bot = bot
        self.bus = Bus(settings)
        self.chats = allowed_chats(settings.telegram_allow)
        self._seen_tasks: set[str] = set()

    # -- outbound ----------------------------------------------------------
    async def broadcast(self, text: str) -> None:
        for chat in sorted(self.chats):
            try:
                await self.bot.send_message(chat, text)
            except Exception:  # noqa: BLE001 - observer must never disturb the mesh
                log.warning("telegram send to %s failed", chat, exc_info=True)

    # -- inbound: NATS events ---------------------------------------------
    async def on_event(self, msg: Msg) -> None:
        try:
            envelope = json.loads(msg.data)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        task_id = envelope.get("task_id", "")
        if task_id and task_id not in self._seen_tasks:
            self._seen_tasks.add(task_id)
            snapshot = await self.bus.get_snapshot(task_id)
            if snapshot is not None:
                await self.broadcast(
                    "New task:\n" + format_task(snapshot.model_dump())
                )
        line = format_event(envelope)
        if line is not None:
            await self.broadcast(line)

    # -- inbound: commands --------------------------------------------------
    async def handle_command(self, chat: str, text: str) -> None:
        parts = text.strip().split(maxsplit=2)
        if not parts:
            return
        cmd = parts[0].lstrip("/").lower()
        if cmd == "tasks":
            snapshots = await self.bus.list_tasks()
            filter_state = parts[1].strip() if len(parts) >= 2 else ""
            await self.bot.send_message(
                chat, summarize_tasks([s.model_dump() for s in snapshots], filter_state)
            )
        elif cmd == "roster":
            nodes = await self.bus.list_nodes()
            await self.bot.send_message(
                chat, format_roster([n.model_dump() for n in nodes])
            )
        elif cmd == "state" and len(parts) >= 2:
            snapshot = await self.bus.get_snapshot(parts[1].lstrip("#").strip())
            if snapshot is None:
                await self.bot.send_message(chat, f"task not found: {parts[1]}")
            else:
                await self.bot.send_message(chat, format_task(snapshot.model_dump()))
        else:
            await self.bot.send_message(
                chat, "commands: tasks [state] | roster | state <task-id>"
            )

    # -- main loops ---------------------------------------------------------
    async def run_events(self) -> None:
        await self.bus.connect()
        assert self.bus.nc is not None
        await self.bus.nc.subscribe(SUBJECT_ALL_EVENTS, cb=self.on_event)
        log.info("telegram observer subscribed to %s", SUBJECT_ALL_EVENTS)
        while True:
            await asyncio.sleep(1.0)

    async def run_commands(self) -> None:
        offset = 0
        while True:
            updates = await self.bot.get_updates(offset)
            for update in updates:
                offset = max(offset, update.get("update_id", 0) + 1)
                message = update.get("message") or {}
                chat_obj = message.get("chat") or {}
                chat = str(chat_obj.get("id", ""))
                if not chat or chat not in self.chats:
                    log.info("ignored message from unauthorized chat %s", chat or "?")
                    continue
                text = message.get("text", "")
                try:
                    await self.handle_command(chat, text)
                except Exception:  # noqa: BLE001
                    log.warning("command failed", exc_info=True)
                    await self.bot.send_message(chat, "error handling command")

    async def run(self) -> None:
        if not self.chats:
            raise SystemExit("TELEGRAM_ALLOW is empty - no chats to mirror to")
        await asyncio.gather(self.run_events(), self.run_commands())


class BotAPI:
    """Minimal async Telegram Bot API client (getUpdates + sendMessage)."""

    def __init__(self, token: str, poll_timeout_s: int = 25):
        self.token = token
        self.poll_timeout_s = poll_timeout_s
        self._client = httpx.AsyncClient(timeout=60.0)

    async def send_message(self, chat: str, text: str) -> None:
        r = await self._client.post(
            f"{API}/bot{self.token}/sendMessage",
            json={"chat_id": chat, "text": text[:4000]},
        )
        r.raise_for_status()

    async def get_updates(self, offset: int) -> list[dict[str, Any]]:
        for attempt in range(3):
            try:
                r = await self._client.get(
                    f"{API}/bot{self.token}/getUpdates",
                    params={"timeout": self.poll_timeout_s, "offset": offset},
                )
                r.raise_for_status()
                return r.json().get("result", [])
            except Exception:  # noqa: BLE001 - retry with backoff
                if attempt == 2:
                    log.warning("getUpdates failed, backing off", exc_info=True)
                    await asyncio.sleep(5.0)
                    return []
                await asyncio.sleep(1.0 * (attempt + 1))
        return []

    async def close(self) -> None:
        await self._client.aclose()


async def amain() -> int:
    settings = Settings()
    logging.basicConfig(level=settings.log_level)
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN not set")
    bot = BotAPI(settings.telegram_bot_token, settings.telegram_poll_s)
    observer = TelegramObserver(settings, bot)
    try:
        await observer.run()
    finally:
        await bot.close()
        await observer.bus.close()
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
