"""T1: Telegram observer (plan §14.6).

Mirrors mesh ledger activity into Telegram and answers read-only commands,
plus the single gated write path for T7.5 model requests: /mreqs lists them,
/approve and /reject decide one and publish the decision on
mesh.model.decision. Nothing else writes. Authorization is a static
allow-list of chat ids (TELEGRAM_ALLOW, comma-separated); anything else is
ignored.

Design notes:
- Uses the raw Bot API over httpx (long polling) - no extra dependency.
- Events are published per task on core NATS subjects
  (mesh.task.<id>.events), so a wildcard core subscription sees all of them.
- The write path is deliberately minimal: only /approve//reject on model
  requests, only for allow-listed chats, always via the KV record + one
  event publish - the mesh itself stays untouched.
- The Bot API calls never raise into the event loop: delivery problems are
  logged and swallowed (an observer must never disturb the mesh).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

import httpx
from nats.aio.msg import Msg

from .bus import Bus
from .config import Settings
from .observations import TaskObservation  # noqa: F401  (format_status typing)
from .schema import (
    SUBJECT_EVENTS,
    ModelRequest,
    model_pull_allowed,
)

log = logging.getLogger(__name__)

API = "https://api.telegram.org"

# Command discovery (P1 item 7): the grouped command list is the single
# source for both /help output and the BotFather setMyCommands registration.
# (group, [(command, description, args), ...]) — args shown in /help only.
COMMAND_GROUPS: list[tuple[str, list[tuple[str, str, str]]]] = [
    (
        "view",
        [
            ("status", "mesh digest", ""),
            ("tasks", "list tasks (optionally by state)", "[state]"),
            ("node", "node detail", "<name>"),
            ("nodes", "roster list", ""),
            ("state", "task state", "<task-id>"),
            ("trace", "debate trace + routing", "<task-id>"),
            ("obslast", "recent observations", "[model] [limit]"),
            ("synergy", "model-pair synergy table", "[limit]"),
            ("mreqs", "model requests", ""),
            ("mrep", "model reputation", ""),
        ],
    ),
    (
        "act",
        [
            ("submit", "new task", "<task>"),
            ("cancel", "cancel task", "<task-id>"),
            ("retry", "rerun task (optionally another model)", "<task-id> [--model <name>]"),
            ("stop worker", "withdraw a node", "<node>"),
        ],
    ),
    (
        "decide",
        [
            ("approve", "approve model request", "<request-id>"),
            ("reject", "reject model request", "<request-id>"),
            ("gate", "verify-gate override", "approve|reject <task-id>"),
        ],
    ),
]


def format_help() -> str:
    lines = ["commands:"]
    for group, commands in COMMAND_GROUPS:
        lines.append(f"{group}:")
        for name, desc, args in commands:
            suffix = f" {args}" if args else ""
            lines.append(f"  /{name}{suffix} - {desc}")
    return "\n".join(lines)


def botfather_commands() -> list[dict[str, str]]:
    """Flattened command list for the BotFather setMyCommands registration."""
    out = []
    for _group, commands in COMMAND_GROUPS:
        for name, desc, _args in commands:
            if " " in name:  # two-word forms are not botfather commands
                continue
            out.append({"command": name, "description": desc})
    return out

SUBJECT_ALL_EVENTS = SUBJECT_EVENTS.format(task_id="*")


def allowed_chats(allow: str) -> set[str]:
    """Parse TELEGRAM_ALLOW: comma-separated ids, '@' and whitespace stripped."""
    return {part.strip().lstrip("@").lower() for part in allow.split(",") if part.strip()}


MIN_TASK_PREFIX = 6

# Quieter digest: progress states that never need operator action. Their
# transitions are suppressed (TELEGRAM_QUIET_PROGRESS, default on); terminal
# states and WAIT_INPUT still broadcast.
ROUTINE_PROGRESS_STATES = {
    "RECEIVED",
    "CLAIMED",
    "PLANNING",
    "CODING",
    "CRITIQUE",
    "REVISE",
}


def resolve_task_id(prefix: str, snapshots: list[dict[str, Any]]) -> tuple[str, list[str]]:
    """Resolve a (possibly short) task-id prefix against known snapshots.

    Returns (resolved_id, candidates). A full exact id always wins (also for
    short strings that ARE an id). A unique startswith match resolves; several
    matches return the sorted candidate list so the caller can ask the user
    to be more specific. Prefixes shorter than MIN_TASK_PREFIX that are not
    an exact id are refused (empty result, no candidates).
    """
    prefix = prefix.lstrip("#").strip()
    if not prefix:
        return "", []
    ids = [str(s.get("task_id", "")) for s in snapshots]
    if prefix in ids:
        return prefix, []
    if len(prefix) < MIN_TASK_PREFIX:
        return "", []
    matches = sorted({i for i in ids if i.startswith(prefix)})
    if len(matches) == 1:
        return matches[0], []
    return "", matches


def inline_keyboard(rows: list[list[tuple[str, str]]]) -> dict[str, Any]:
    """Build Telegram reply_markup for an inline keyboard."""
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": data} for label, data in row] for row in rows
        ]
    }


def task_buttons(task_id: str) -> dict[str, Any]:
    """[Retry][Cancel] buttons for a task card (14.11 P0)."""
    return inline_keyboard(
        [
            [
                ("Retry", f"tk:{task_id}:retry"),
                ("Cancel", f"tk:{task_id}:cancel"),
            ]
        ]
    )


def mreq_buttons(request_id: str) -> dict[str, Any]:
    """[Approve][Reject] buttons for a pending model-request card."""
    return inline_keyboard(
        [
            [
                ("Approve", f"mr:{request_id}:approve"),
                ("Reject", f"mr:{request_id}:reject"),
            ]
        ]
    )


def parse_callback(data: str) -> tuple[str, str, str] | None:
    """Parse callback_data into (kind, id, action).

    Kinds: tk = task (retry/cancel), mr = model request (approve/reject),
    nd = node (pause/resume). Anything malformed or with an unknown action
    returns None — callback_data is untrusted input.
    """
    parts = data.split(":")
    if len(parts) != 3:
        return None
    kind, ident, action = parts
    ident = ident.strip()
    if not ident:
        return None
    if kind == "tk" and action in ("retry", "cancel"):
        return "task", ident, action
    if kind == "mr" and action in ("approve", "reject"):
        return "mreq", ident, action
    if kind == "nd" and action in ("pause", "resume"):
        return "node", ident, action
    return None


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


def format_done_card(snapshot_dict: dict[str, Any]) -> str:
    """Completion card that carries the result itself (P1 item 5): title,
    coder summary excerpt and produced files - no second tool hop needed."""
    task_id = snapshot_dict.get("task_id", "?")
    title = (snapshot_dict.get("task") or "").strip()
    title = title.splitlines()[0][:70] if title else ""
    card = f"DONE #{task_id[:12]}"
    if title:
        card += f"\n{title}"
    code = snapshot_dict.get("code") or {}
    summary = (code.get("summary") or "").strip()
    if summary:
        card += f"\nresult: {summary[:400]}"
    files = [f.get("path") for f in (code.get("files") or []) if f.get("path")]
    if files:
        card += f"\nfiles: {', '.join(files[:5])}"
    return card


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


def format_trace(snapshot_dict: dict[str, Any], max_entries: int = 25) -> str:
    """T5: replay the coder/critic debate from the snapshot history."""
    tid = str(snapshot_dict.get("task_id", "?"))[:12]
    state = snapshot_dict.get("state", "?")
    lines = [f"#trace {tid} ({state}, rev{snapshot_dict.get('revisions', 0)})"]
    title = (snapshot_dict.get("task") or "").strip().splitlines()[0][:60]
    if title:
        lines.append(f"task: {title}")
    # T4 (model-routing-visible): which node:model ran each role step.
    for role_name, target in (snapshot_dict.get("route") or {}).items():
        lines.append(f"route {role_name}: {target}")
    entries = snapshot_dict.get("history") or []
    if not entries:
        lines.append("no history recorded")
        return "\n".join(lines)
    if len(entries) > max_entries:
        lines.append(f"({len(entries)} entries, last {max_entries} shown)")
    for entry in entries[-max_entries:]:
        detail = str(entry.get("detail", "")).replace("\n", " ").strip()
        line = (
            f"- {entry.get('event', '?')}: {detail}" if detail else (f"- {entry.get('event', '?')}")
        )
        lines.append(line[:220])
    return "\n".join(lines)


def summarize_tasks(
    snapshots: list[dict[str, Any]], filter_state: str = "", limit: int = 15
) -> str:
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


def format_status(
    snapshots: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    requests: list[ModelRequest],
    observations: list[TaskObservation],
) -> str:
    """14.11 P0 one-shot digest: is everything okay?"""
    counts: dict[str, int] = {}
    for s in snapshots:
        counts[s.get("state", "?")] = counts.get(s.get("state", "?"), 0) + 1
    lines = [
        f"{len(snapshots)} tasks: "
        + " ".join(f"{state}={n}" for state, n in sorted(counts.items(), key=lambda kv: -kv[1]))
    ]
    active = [s for s in snapshots if s.get("state") not in ("DONE", "FAILED", "CANCELLED")]
    if active:
        parts = [
            f"#{str(s.get('task_id', '?'))[:12]} {s.get('state', '?')}"
            f" ({s.get('claimed_by') or 'unclaimed'})"
            for s in active[:5]
        ]
        lines.append("active: " + "; ".join(parts))
    else:
        lines.append("active: none")
    if nodes:
        lines.append("nodes: " + ", ".join(n.get("node_id", "?") for n in nodes[:8]))
    else:
        lines.append("nodes: none (mesh empty)")
    open_reqs = [r for r in requests if getattr(r, "status", "") == "pending"]
    lines.append(
        f"model requests: {len(open_reqs)} pending" if open_reqs else "model requests: none pending"
    )
    recent_failed = [o for o in observations if o.outcome != "done"]
    if recent_failed:
        lines.append("recent failures:")
        for o in sorted(recent_failed, key=lambda o: o.observed_at, reverse=True)[:3]:
            route = ",".join(f"{role}:{o.route[role]}" for role in sorted(o.route)) or "-"
            lines.append(f"  {o.task_id[:12]} {o.outcome} | {route} | {o.observed_at}")
    else:
        lines.append("recent failures: none")
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
            f"{n.get('node_id', '?')} roles={roles} models={models} vram={vram:g}GB gpu={gpu}"
        )
    return "\n".join(lines)


def format_node_detail(data: dict[str, Any]) -> str:
    """M11a /node <name>: which node, what it runs, which tasks it holds,
    with each task's content (first line = the actual instruction)."""
    node = data.get("node") or {}
    roles = ",".join(node.get("roles") or []) or "-"
    models = ",".join(node.get("models") or []) or "-"
    lines = [
        f"node {node.get('node_id', '?')} roles={roles} models={models} "
        f"gpu={node.get('gpu') or '-'}"
    ]
    tasks = data.get("tasks") or []
    if not tasks:
        lines.append("no tasks claimed")
        return "\n".join(lines)
    lines.append(f"{len(tasks)} tasks:")
    for t in tasks:
        title = (t.get("task") or "").splitlines()[0][:70]
        claimed = t.get("claimed_by") or "-"
        lines.append(
            f"  #{t.get('task_id', '?')[:12]} {t.get('state', '?')} by {claimed}\n    {title}"
        )
    return "\n".join(lines)


def format_model_requests(requests: list[ModelRequest]) -> str:
    """Compact /mreqs view: one line per model request, newest first."""
    if not requests:
        return "no model requests"
    lines = [f"{len(requests)} model requests:"]
    for r in requests[:15]:
        disk = f" disk@{','.join(r.nodes_disk)}" if r.nodes_disk else ""
        lines.append(f"{r.request_id} {r.status} {r.action} {r.model}{disk}")
    return "\n".join(lines)


def failure_suggestion(snapshot) -> str:
    """P2 item 9: the orchestrator's alternative-model suggestion from the
    snapshot history (last ``model.suggest`` entry), if any. Duck-typed:
    snapshot-like fakes without a history just yield no suggestion."""
    history = getattr(snapshot, "history", None) or []
    for entry in reversed(history):
        if getattr(entry, "event", None) == "model.suggest":
            return f"next try: {entry.detail}"
    return ""


def _redact(text: str, token: str) -> str:
    """Strip the bot token out of exception strings (URLs leak it)."""
    if not token:
        return text
    return text.replace(token, "[REDACTED]")


class TelegramObserver:
    """NATS events -> Telegram; read commands plus the ONE gated write path
    (approve/reject on model requests, plan §14.7)."""

    def __init__(self, settings: Settings, bot: BotAPI):
        self.settings = settings
        self.bot = bot
        self.bus = Bus(settings)
        self.chats = allowed_chats(settings.telegram_allow)
        self._seen_tasks: set[str] = set()
        self._last_state: dict[str, str] = {}
        self._announced: set[tuple[str, str, str]] = set()
        self._seen_nodes: set[str] = set()

    # -- outbound ----------------------------------------------------------
    async def broadcast(self, text: str, markup: dict[str, Any] | None = None) -> None:
        for chat in sorted(self.chats):
            try:
                if markup is None:
                    await self.bot.send_message(chat, text)
                else:
                    await self.bot.send_message(chat, text, markup)
            except Exception as exc:  # noqa: BLE001 - observer must never disturb the mesh
                log.warning(
                    "telegram send to %s failed: %s",
                    chat,
                    _redact(str(exc), getattr(self.bot, "token", "")),
                )

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
                await self.broadcast("New task:\n" + format_task(snapshot.model_dump()))
        if self._digest_event(envelope):
            line = format_event(envelope)
            if line is not None:
                payload = envelope.get("payload") or {}
                state = (payload.get("state") or envelope.get("state") or "").upper()
                if state in ("FAILED", "ERROR") and task_id:
                    snapshot = await self.bus.get_snapshot(task_id)
                    title = ""
                    if snapshot is not None:
                        title = (snapshot.task or "").strip().splitlines()[0][:70]
                    detail = (payload.get("detail", "") or "").strip()
                    card = f"FAILED #{task_id[:12]}"
                    if title:
                        card += f"\n{title}"
                    if detail:
                        card += f"\ncause: {detail}"
                    if snapshot is not None:
                        # P2 item 9: surface the orchestrator's alternative-
                        # model suggestion from the snapshot history.
                        suggestion = failure_suggestion(snapshot)
                        if suggestion:
                            card += f"\n{suggestion}"
                    card += f"\nretry: /retry {task_id}"
                    await self.broadcast(card, markup=task_buttons(task_id))
                elif state == "DONE" and task_id:
                    # P1 item 5: deliver the result, not just the ledger state.
                    snapshot = await self.bus.get_snapshot(task_id)
                    if snapshot is not None:
                        await self.broadcast(
                            format_done_card(snapshot.model_dump())
                        )
                    else:
                        await self.broadcast(line)
                else:
                    await self.broadcast(line)

    def _digest_event(self, envelope: dict[str, Any]) -> bool:
        """True when this event is worth announcing: a task STATE TRANSITION
        (claimed / debate / done / failed) or a NEW failure detail. Routine
        per-step chatter (dispatch, result.executed_on, retries inside the
        same state) is suppressed so the channel stays a digest."""
        if getattr(self.settings, "telegram_verbose", False):
            return True
        payload = envelope.get("payload") or {}
        state = payload.get("state") or envelope.get("state") or "?"
        task_id = envelope.get("task_id", "")
        if not task_id:
            return False
        # Quieter digest (P1 item 8): routine progress transitions are dropped
        # entirely -- the New-task card already announced the task, and the
        # terminal/action states (DONE/FAILED/CANCELLED/WAIT_INPUT) still
        # broadcast, so nothing actionable is lost.
        quiet = getattr(self.settings, "telegram_quiet_progress", True)
        if quiet and state.upper() in ROUTINE_PROGRESS_STATES:
            self._last_state[task_id] = state
            return False
        if state != self._last_state.get(task_id):
            self._last_state[task_id] = state
            detail = (payload.get("detail", "") or "").strip()
            if state in ("FAILED", "ERROR") and detail:
                self._announced.add((task_id, state, detail[:120]))
            return True
        detail = (payload.get("detail", "") or "").strip()
        if state in ("FAILED", "ERROR") and detail:
            key = (task_id, state, detail[:120])
            if key not in self._announced:
                self._announced.add(key)
                return True
        return False

    async def run_roster(self) -> None:
        """Poll the node roster and announce mesh membership changes."""
        await self.bus.connect()
        while True:
            try:
                nodes = await self.bus.list_nodes()
                current = {n.node_id for n in nodes}
                joined = sorted(current - self._seen_nodes)
                left = sorted(self._seen_nodes - current)
                # Roster churn is mostly lease noise (agents re-register on
                # the next heartbeat); only surface it when explicitly asked.
                if getattr(self.settings, "telegram_roster_updates", False) and (
                    self._seen_nodes and (joined or left)
                ):
                    parts = []
                    if joined:
                        parts.append("joined: " + ", ".join(joined))
                    if left:
                        parts.append("left: " + ", ".join(left))
                    await self.broadcast("mesh: " + "; ".join(parts))
                self._seen_nodes = current
            except Exception as exc:  # noqa: BLE001 - observer must never disturb the mesh
                log.warning(
                    "roster poll failed: %s", _redact(str(exc), getattr(self.bot, "token", ""))
                )
            await asyncio.sleep(max(self.settings.telegram_poll_s, 5))

    # -- inbound: commands --------------------------------------------------
    async def handle_command(self, chat: str, text: str) -> None:
        parts = text.strip().split(maxsplit=2)
        if not parts:
            return
        cmd = parts[0].lstrip("/").lower()
        if cmd == "status":
            snapshots = await self.bus.list_tasks()
            nodes = await self.bus.list_nodes()
            requests = await self.bus.list_model_requests()
            observations = await self.bus.list_observations()
            await self.bot.send_message(
                chat,
                format_status(
                    [s.model_dump() for s in snapshots],
                    [n.model_dump() for n in nodes],
                    requests,
                    observations,
                ),
            )
        elif cmd == "tasks":
            snapshots = await self.bus.list_tasks()
            filter_state = parts[1].strip() if len(parts) >= 2 else ""
            await self.bot.send_message(
                chat, summarize_tasks([s.model_dump() for s in snapshots], filter_state)
            )
        elif cmd == "obslast":
            from memnc.observations import format_observations

            observations = await self.bus.list_observations()
            model = parts[1].strip() if len(parts) >= 2 else None
            await self.bot.send_message(chat, format_observations(observations, model))
        elif cmd == "synergy":
            from memnc.synergy import compute_synergy_metrics, format_synergy

            observations = await self.bus.list_observations()
            limit = 10
            if len(parts) >= 2 and parts[1].strip().isdigit():
                limit = int(parts[1].strip())
            await self.bot.send_message(
                chat, format_synergy(compute_synergy_metrics(observations), limit)
            )
        elif cmd in ("node", "nodes"):
            # M11a: query the mesh API, not local state — this is the
            # first Telegram command routed through mesh.api.request.*.
            # /node <name> = detail; /node or /nodes = list.
            try:
                from .api import api_request

                if len(parts) >= 2:
                    reply = await api_request(self.bus, "node.detail", node_id=parts[1].strip())
                    await self.bot.send_message(chat, format_node_detail(reply["data"]))
                else:
                    reply = await api_request(self.bus, "node.list")
                    await self.bot.send_message(chat, format_roster(reply["data"]))
            except RuntimeError as exc:
                await self.bot.send_message(chat, str(exc))
        elif cmd == "roster":
            nodes = await self.bus.list_nodes()
            await self.bot.send_message(chat, format_roster([n.model_dump() for n in nodes]))
        elif cmd == "trace" and len(parts) >= 2:
            tid = await self._resolve_task(chat, parts[1])
            if tid:
                snapshot = await self.bus.get_snapshot(tid)
                if snapshot is None:
                    await self.bot.send_message(chat, f"task not found: {parts[1]}")
                else:
                    await self.bot.send_message(chat, format_trace(snapshot.model_dump()))
        elif cmd == "state" and len(parts) >= 2:
            tid = await self._resolve_task(chat, parts[1])
            if tid:
                snapshot = await self.bus.get_snapshot(tid)
                if snapshot is None:
                    await self.bot.send_message(chat, f"task not found: {parts[1]}")
                else:
                    await self.bot.send_message(chat, format_task(snapshot.model_dump()))
        elif cmd == "mreqs":
            requests = await self.bus.list_model_requests()
            await self.bot.send_message(chat, format_model_requests(requests))
        elif cmd == "mrep":
            # T7: model reputation (critic verdict streaks, retirements).
            reps = await self.bus.list_reputation()
            if not reps:
                await self.bot.send_message(chat, "no reputation entries")
            else:
                lines = ["model | appr | rej | streak | retired"]
                for r in reps:
                    lines.append(
                        f"{r.model} | {r.approvals} | {r.rejections} "
                        f"| {r.consecutive_rejections} "
                        f"| {'yes' if r.retired else 'no'}"
                    )
                await self.bot.send_message(chat, "\n".join(lines))
        elif cmd in ("approve", "reject") and len(parts) >= 2:
            await self._decide(chat, cmd, parts[1].lstrip("#").strip())
        elif cmd == "gate" and len(parts) >= 3 and parts[1] in ("approve", "reject"):
            # T3 task approval gate (distinct from T7.5 /approve model requests)
            tid = await self._resolve_task(chat, parts[2])
            if tid:
                await self._control(chat, "decide", f"{tid} {parts[1]}")
        elif cmd in ("submit", "cancel", "retry"):
            argument = parts[1] if len(parts) >= 2 else ""
            if cmd == "retry" and len(parts) >= 3:
                # Retry flags ride in the third split part ("--model <name>").
                argument = f"{argument} {parts[2]}"
            model_override = ""
            if cmd == "retry":
                # Retry-with-intent: "/retry <id> --model <name>" reruns the
                # task on a different model; plain "/retry <id>" keeps the old.
                words = argument.split()
                if "--model" in words:
                    idx = words.index("--model")
                    if idx + 1 < len(words):
                        model_override = words[idx + 1]
                        words = words[:idx] + words[idx + 2 :]
                    else:
                        model_override = "__missing__"
                if words:
                    argument = words[0]
            if cmd in ("cancel", "retry") and argument:
                tid = await self._resolve_task(chat, argument)
                if not tid:
                    return
                argument = tid
            if model_override == "__missing__":
                await self.bot.send_message(chat, "usage: /retry <task-id> --model <name>")
                return
            await self._control(chat, cmd, argument, model_override=model_override)
        elif cmd == "stop" and len(parts) >= 3 and parts[1].lower() == "worker":
            await self._control(chat, "stop_worker", parts[2])
        else:
            await self.bot.send_message(chat, format_help())

    # -- T2: human intervention — Telegram is an M11b API client --------------
    async def _resolve_task(self, chat: str, prefix: str) -> str:
        """Resolve a task-id prefix; reply in-chat when it does not resolve.

        Returns the full task id, or "" after telling the user why (unknown
        id, too short, ambiguous). Full ids pass through unchanged.
        """
        snapshots = [s.model_dump() for s in await self.bus.list_tasks()]
        tid, candidates = resolve_task_id(prefix, snapshots)
        if tid:
            return tid
        if candidates:
            shown = ", ".join(f"#{c[:12]}" for c in candidates[:5])
            more = f" (+{len(candidates) - 5} more)" if len(candidates) > 5 else ""
            await self.bot.send_message(chat, f"ambiguous prefix {prefix}: {shown}{more}")
        elif len(prefix.lstrip('#').strip()) < MIN_TASK_PREFIX:
            await self.bot.send_message(
                chat, f"prefix too short ({prefix}) — use at least {MIN_TASK_PREFIX} chars"
            )
        else:
            await self.bot.send_message(chat, f"task not found: {prefix}")
        return ""

    async def handle_callback(self, chat: str, message_id: int | None, data: str) -> None:
        """Route an inline-button press through the existing gated paths.

        callback_data is untrusted input: only the fixed verb table of
        parse_callback is accepted, and every write still rides the same
        _control/_decide grant-checked surfaces as the typed commands.
        """
        parsed = parse_callback(data)
        if parsed is None:
            await self.bot.send_message(chat, "unknown button action")
            return
        kind, ident, action = parsed
        try:
            if kind == "task":
                await self._control(chat, action, ident)
            elif kind == "mreq":
                await self._decide(chat, action, ident)
            elif kind == "node":
                await self._control(chat, f"{action}_node", ident)
        finally:
            # Best-effort stamp on the card so the decision stays visible.
            if message_id is not None:
                try:
                    await self.bot.edit_message_text(chat, message_id, f"{data} -> sent")
                except Exception:  # noqa: BLE001 - cosmetic only
                    pass

    async def _control(
        self, chat: str, action: str, argument: str, model_override: str = ""
    ) -> None:
        """Send the command through the mesh API write path.

        The API server forwards it to the orchestrator's mesh.control gate
        (grant-checked there, deny-by-default). This keeps Telegram a pure
        API client — one verb surface for every frontend.
        """
        from .api import api_request

        verb = {
            "submit": "task.submit",
            "cancel": "task.cancel",
            "retry": "task.retry",
            "decide": "task.decide",
            "stop_worker": "node.stop_worker",
            "pause_node": "node.pause_node",
            "resume_node": "node.resume_node",
        }[action]
        args: dict[str, Any] = {
            "agent_id": f"telegram-{chat}",  # KV keys reject ':'
        }
        if action == "submit":
            args["task"] = argument
        elif action == "stop_worker":
            args["node"] = argument
        elif action == "decide":
            words = argument.split()
            args["task_id"] = words[0].lstrip("#") if words else ""
            args["approved"] = len(words) >= 2 and words[1] == "approve"
        elif action in ("cancel", "retry"):
            args["task_id"] = argument.split()[0].lstrip("#")
            if model_override:
                args["model_override"] = model_override
        else:
            args["node"] = argument
        try:
            resp = await api_request(self.bus, verb, timeout=20, **args)
            if action == "submit":
                await self.bot.send_message(chat, f"submitted: #{resp.get('task_id')}")
            else:
                await self.bot.send_message(chat, f"{action} ok")
        except RuntimeError as exc:
            await self.bot.send_message(chat, f"denied: {exc}")
        except Exception as exc:  # noqa: BLE001
            await self.bot.send_message(chat, f"control failed: {exc}")

    # -- T7.5: minimal model-request write path -----------------------------
    async def _decide(self, chat: str, action: str, request_id: str) -> None:
        """The ONLY write path: approve/reject on a model request."""
        status = "approved" if action == "approve" else "rejected"
        matches = [
            r for r in await self.bus.list_model_requests() if r.request_id.startswith(request_id)
        ]
        if not matches:
            await self.bot.send_message(chat, f"no model request: {request_id}")
            return
        request = matches[0]
        if request.status != "pending":
            await self.bot.send_message(chat, f"{request.request_id} already {request.status}")
            return
        # T7: an approved pull is refused when the model is not whitelisted.
        if (
            status == "approved"
            and request.action == "pull"
            and not model_pull_allowed(request.model, self.settings.model_pull_whitelist)
        ):
            await self.bot.send_message(
                chat, f"refused: {request.model} not in model_pull_whitelist"
            )
            return
        request.status = status
        request.decided_by = f"telegram:{chat}"
        request.decided_at = datetime.now(UTC).isoformat()
        await self.bus.put_model_request(request)
        await self.bus.publish_model_decision(request)
        await self.bot.send_message(chat, f"{request.request_id} {status} ({request.model})")

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
                cq = update.get("callback_query")
                if cq:
                    await self._run_callback(cq)
                    continue
                message = update.get("message") or {}
                chat_obj = message.get("chat") or {}
                chat = str(chat_obj.get("id", ""))
                if not chat or chat not in self.chats:
                    log.info("ignored message from unauthorized chat %s", chat or "?")
                    continue
                text = message.get("text", "")
                try:
                    await self.handle_command(chat, text)
                except Exception as exc:  # noqa: BLE001
                    log.warning("command failed: %s", _redact(str(exc), self.bot.token))
                    await self.bot.send_message(chat, "error handling command")

    async def _run_callback(self, cq: dict[str, Any]) -> None:
        message = cq.get("message") or {}
        chat = str((message.get("chat") or {}).get("id", ""))
        if not chat or chat not in self.chats:
            # callback_data is attacker-influenced: answer nothing else to a
            # chat outside TELEGRAM_ALLOW.
            log.info("ignored callback from unauthorized chat %s", chat or "?")
            try:
                await self.bot.answer_callback(cq.get("id", ""))
            except Exception:  # noqa: BLE001 - cosmetic
                pass
            return
        try:
            await self.bot.answer_callback(cq.get("id", ""))
            await self.handle_callback(
                chat, message.get("message_id"), str(cq.get("data", ""))
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("callback failed: %s", _redact(str(exc), self.bot.token))
            await self.bot.send_message(chat, "error handling button")

    async def run(self) -> None:
        if not self.chats:
            raise SystemExit("TELEGRAM_ALLOW is empty - no chats to mirror to")
        try:
            await self.bot.set_my_commands(botfather_commands())
        except Exception as exc:  # noqa: BLE001 - registration is best-effort
            log.warning("setMyCommands failed: %s", _redact(str(exc), self.bot.token))
        await asyncio.gather(self.run_events(), self.run_commands(), self.run_roster())


class BotAPI:
    """Minimal async Telegram Bot API client (getUpdates + sendMessage)."""

    def __init__(self, token: str, poll_timeout_s: int = 25):
        self.token = token
        self.poll_timeout_s = poll_timeout_s
        self._client = httpx.AsyncClient(timeout=60.0)

    async def send_message(self, chat: str, text: str, markup: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"chat_id": chat, "text": text[:4000]}
        if markup is not None:
            payload["reply_markup"] = markup
        r = await self._client.post(
            f"{API}/bot{self.token}/sendMessage",
            json=payload,
        )
        r.raise_for_status()

    async def set_my_commands(self, commands: list[dict[str, str]]) -> None:
        """Register the command list with BotFather (Telegram autocomplete)."""
        r = await self._client.post(
            f"{API}/bot{self.token}/setMyCommands",
            json={"commands": commands},
        )
        r.raise_for_status()

    async def answer_callback(self, callback_query_id: str, text: str = "") -> None:
        """Acknowledge a button press (stops the client-side spinner)."""
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        r = await self._client.post(
            f"{API}/bot{self.token}/answerCallbackQuery",
            json=payload,
        )
        r.raise_for_status()

    async def edit_message_text(self, chat: str, message_id: int, text: str) -> None:
        r = await self._client.post(
            f"{API}/bot{self.token}/editMessageText",
            json={"chat_id": chat, "message_id": message_id, "text": text[:4000]},
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
            except Exception as exc:  # noqa: BLE001 - retry with backoff
                if attempt == 2:
                    log.warning("getUpdates failed, backing off: %s", _redact(str(exc), self.token))
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
