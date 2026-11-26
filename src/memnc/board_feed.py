"""Board feeder: move roadmap tasks into the mesh pipeline and results back.

The board (``docs/roadmap.md``) is the human queue — what should be done. The
ledger is the pipeline — what is running. This module bridges the two on every
tick:

* reconcile: a mesh task that reached DONE marks its board row COMPLETED; a
  failed one goes back to SUBMITTED (up to ``max_attempts``, then DEFERRED).
* fill: while fewer than ``capacity`` board tasks are in flight, the next open
  task (P0 before P1 before P2) is submitted and marked WORKING.

Dispatch modes:

* ``pull`` — the task is submitted claimable (M11), so a worker CAS-claims it
  itself. This is the "mesh holt sich die Tasks selber" mode.
* ``push`` — the orchestrator dispatches it like any other task.

The feeder never touches a row a human moved away from WORKING, and it keeps
its bookkeeping in a small JSON file so a restart resumes the same tasks.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .fsutil import atomic_write
from .schema import TERMINAL_STATES, TaskSnapshot, TaskState

log = logging.getLogger(__name__)

PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}
OPEN_STATE = "SUBMITTED"
WORKING = "WORKING"
DEFAULT_CAPACITY = 1
DEFAULT_MAX_ATTEMPTS = 3
EVIDENCE_CHARS = 1200


class BoardSource(Protocol):
    """The three board verbs the feeder needs (MCP server or in-process)."""

    async def list(self, state: str = "") -> list[dict]: ...

    async def get(self, task_id: str) -> dict: ...

    async def set_state(self, task_id: str, state: str) -> dict: ...


@dataclass(frozen=True)
class BoardTask:
    id: str
    title: str
    priority: str = ""
    state: str = OPEN_STATE
    evidence: str = ""


@dataclass
class TickResult:
    started: list[str] = field(default_factory=list)
    completed: list[str] = field(default_factory=list)
    requeued: list[str] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)
    running: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "started": self.started,
            "completed": self.completed,
            "requeued": self.requeued,
            "deferred": self.deferred,
            "running": self.running,
            "errors": self.errors,
        }


def board_tasks(rows: list[dict]) -> list[BoardTask]:
    """Parse board rows, dropping malformed ones and any error entries."""
    out: list[BoardTask] = []
    for row in rows:
        if not isinstance(row, dict) or "error" in row or not row.get("id"):
            continue
        out.append(
            BoardTask(
                id=str(row["id"]),
                title=str(row.get("title", "")).strip(),
                priority=str(row.get("priority", "") or ""),
                state=str(row.get("state", "") or OPEN_STATE),
                evidence=str(row.get("evidence", "") or ""),
            )
        )
    return out


def open_tasks(rows: list[dict]) -> list[BoardTask]:
    """Open board tasks in dispatch order: priority first, then board order."""
    tasks = [task for task in board_tasks(rows) if task.state == OPEN_STATE]
    return sorted(tasks, key=lambda task: PRIORITY_ORDER.get(task.priority, 99))


def task_text(task: BoardTask) -> str:
    """The prompt the mesh receives for a board task."""
    evidence = " ".join(task.evidence.split())[:EVIDENCE_CHARS]
    lines = [
        f"[Board {task.id}] {task.title}",
        "",
        f"Priority: {task.priority or 'unset'}.",
        "This task comes from the roadmap board (docs/roadmap.md). Do not edit "
        "the board yourself: the feeder marks it WORKING and COMPLETED.",
    ]
    if evidence:
        lines += ["", f"Board evidence: {evidence}"]
    return "\n".join(lines)


class FeedStore:
    """JSON bookkeeping: board id -> mesh task id, plus attempt counts."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {"in_flight": {}, "attempts": {}}
        if not isinstance(data, dict):
            return {"in_flight": {}, "attempts": {}}
        data.setdefault("in_flight", {})
        data.setdefault("attempts", {})
        if not isinstance(data["in_flight"], dict):
            data["in_flight"] = {}
        if not isinstance(data["attempts"], dict):
            data["attempts"] = {}
        return data

    def save(self, data: dict) -> None:
        atomic_write(self.path, json.dumps(data, indent=2, sort_keys=True) + "\n")


class BoardFeeder:
    """One reconcile+fill tick, with injectable board/submit/snapshot hooks."""

    def __init__(
        self,
        board: BoardSource,
        submit: Callable[[str], Awaitable[str]],
        snapshot: Callable[[str], Awaitable[TaskSnapshot | None]],
        store: FeedStore,
        capacity: int = DEFAULT_CAPACITY,
        mode: str = "pull",
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        now: Callable[[], float] = time.time,
        dry_run: bool = False,
    ):
        if mode not in {"pull", "push"}:
            raise ValueError("mode must be 'pull' or 'push'")
        self.board = board
        self.submit = submit
        self.snapshot = snapshot
        self.store = store
        self.capacity = max(1, capacity)
        self.mode = mode
        self.max_attempts = max(1, max_attempts)
        self.now = now
        self.dry_run = dry_run

    async def tick(self) -> TickResult:
        result = TickResult()
        state = self.store.load()
        in_flight: dict = state["in_flight"]
        attempts: dict = state["attempts"]

        rows = await self.board.list()
        if self.dry_run:
            # Preview only: report what would start, change nothing.
            result.started = [task.id for task in open_tasks(rows)[: self.capacity]]
            return result
        by_id = {task.id: task for task in board_tasks(rows)}

        # 1. Reconcile what we started earlier.
        for board_id, entry in list(in_flight.items()):
            task_id = str(entry.get("task_id", "")) if isinstance(entry, dict) else ""
            row = by_id.get(board_id)
            if not task_id:
                in_flight.pop(board_id, None)
                continue
            # A row a human moved off WORKING wins over our bookkeeping.
            if row is None or row.state not in {WORKING, OPEN_STATE}:
                in_flight.pop(board_id, None)
                continue
            snapshot = await self.snapshot(task_id)
            if snapshot is None:
                # Ledger lost the task (wipe/restore): give the slot back.
                in_flight.pop(board_id, None)
                await self._requeue(board_id, result, attempts, reason="task missing")
                continue
            if snapshot.state == TaskState.DONE:
                in_flight.pop(board_id, None)
                attempts.pop(board_id, None)
                await self._set_state(board_id, "COMPLETED", result)
                result.completed.append(board_id)
            elif snapshot.state in TERMINAL_STATES:
                in_flight.pop(board_id, None)
                reason = f"mesh task {snapshot.state.value.lower()}"
                await self._requeue(board_id, result, attempts, reason=reason)
            else:
                # Crash window: submitted but the board write did not land.
                if row is not None and row.state == OPEN_STATE:
                    await self._set_state(board_id, WORKING, result)
                result.running.append(
                    {"board": board_id, "task_id": task_id, "state": snapshot.state.value}
                )

        # 2. Fill free lanes.
        free = self.capacity - len(in_flight)
        if free > 0:
            for task in open_tasks(rows):
                if free <= 0:
                    break
                if task.id in in_flight:
                    continue
                if int(attempts.get(task.id, 0)) >= self.max_attempts:
                    result.deferred.append(task.id)
                    continue
                await self._start(task, in_flight, state, result)
                free -= 1

        self.store.save(state)
        return result

    async def _start(
        self, task: BoardTask, in_flight: dict, state: dict, result: TickResult
    ) -> None:
        try:
            task_id = await self.submit(task_text(task))
        except Exception as exc:  # noqa: BLE001 - one bad task must not stop the tick
            log.warning("board %s submit failed: %s", task.id, exc)
            result.errors.append(f"{task.id}: submit failed: {exc}")
            return
        in_flight[task.id] = {"task_id": task_id, "submitted": self.now()}
        self.store.save(state)
        await self._set_state(task.id, WORKING, result)
        result.started.append(task.id)
        log.info("board %s -> mesh task %s (%s)", task.id, task_id, self.mode)

    async def _requeue(
        self, board_id: str, result: TickResult, attempts: dict, reason: str
    ) -> None:
        count = int(attempts.get(board_id, 0)) + 1
        attempts[board_id] = count
        if count >= self.max_attempts:
            await self._set_state(board_id, "DEFERRED", result)
            result.deferred.append(board_id)
            log.warning("board %s deferred after %d attempts (%s)", board_id, count, reason)
            return
        await self._set_state(board_id, OPEN_STATE, result)
        result.requeued.append(board_id)

    async def _set_state(self, board_id: str, new_state: str, result: TickResult) -> bool:
        try:
            reply = await self.board.set_state(board_id, new_state)
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"{board_id}: board write failed: {exc}")
            log.warning("board %s state %s failed: %s", board_id, new_state, exc)
            return False
        if isinstance(reply, dict) and "error" in reply:
            result.errors.append(f"{board_id}: {reply['error']}")
            return False
        return True


class InProcessBoard:
    """BoardSource over memnc.board — same file, no subprocess."""

    def __init__(self, path: Path, write: bool = True):
        self.path = Path(path)
        self.write = write

    def _rows(self) -> list[dict]:
        from . import board as board_mod

        text = self.path.read_text(encoding="utf-8")
        return [
            {"id": task_id, "title": title, "state": state, "priority": priority}
            for task_id, title, state, priority in board_mod.tasks(text)
        ]

    async def list(self, state: str = "") -> list[dict]:
        return [row for row in self._rows() if not state or row["state"] == state]

    async def get(self, task_id: str) -> dict:
        from . import board as board_mod

        text = self.path.read_text(encoding="utf-8")
        try:
            return board_mod.get_task(text, task_id)
        except KeyError:
            return {"error": f"no task {task_id} on the board"}

    async def set_state(self, task_id: str, state: str) -> dict:
        from . import board as board_mod

        if not self.write:
            return {"error": "board writes are disabled"}
        text = self.path.read_text(encoding="utf-8")
        try:
            new_text = board_mod.set_state(text, task_id, state)
        except (KeyError, ValueError) as exc:
            return {"error": str(exc)}
        atomic_write(self.path, board_mod.updated(new_text))
        return {"id": task_id, "state": state}


class MCPBoard:
    """BoardSource over the board MCP server (stdio), as the operator runs it."""

    def __init__(self, roadmap: Path | None = None, write: bool = True):
        self.roadmap = Path(roadmap) if roadmap else None
        self.write = write
        self._stack = None
        self._session = None

    async def __aenter__(self) -> MCPBoard:
        import os
        import sys
        from contextlib import AsyncExitStack

        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        env = {
            key: os.environ[key]
            for key in ("PATH", "HOME", "LANG", "TMPDIR", "PYTHONPATH", "VIRTUAL_ENV")
            if key in os.environ
        }
        if self.write:
            env["BOARD_WRITE"] = "1"
        if self.roadmap is not None:
            env["BOARD_ROADMAP"] = str(self.roadmap)
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "memnc.mcp.board_server"], env=env
        )
        self._stack = AsyncExitStack()
        read, write = await self._stack.enter_async_context(stdio_client(params))
        self._session = await self._stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()
        return self

    async def __aexit__(self, *exc_info) -> None:
        assert self._stack is not None
        await self._stack.aclose()

    async def _call(self, tool: str, arguments: dict) -> object:
        assert self._session is not None, "use 'async with MCPBoard(...)'"
        result = await self._session.call_tool(tool, arguments)
        structured = getattr(result, "structuredContent", None)
        if isinstance(structured, dict):
            # MCPServer wraps list returns as {"result": [...]}
            return structured.get("result", structured)
        texts = [
            block.text
            for block in getattr(result, "content", [])
            if getattr(block, "text", None) is not None
        ]
        raw = "\n".join(texts).strip()
        try:
            return json.loads(raw)
        except ValueError:
            return {"error": raw or "empty MCP reply"}

    async def list(self, state: str = "") -> list[dict]:
        payload = await self._call("board_list", {"state": state} if state else {})
        rows = payload if isinstance(payload, list) else [payload]
        return [row for row in rows if isinstance(row, dict)]

    async def get(self, task_id: str) -> dict:
        payload = await self._call("board_get", {"task_id": task_id})
        return payload if isinstance(payload, dict) else {"error": "bad MCP reply"}

    async def set_state(self, task_id: str, state: str) -> dict:
        payload = await self._call("board_set_state", {"task_id": task_id, "state": state})
        return payload if isinstance(payload, dict) else {"error": "bad MCP reply"}
