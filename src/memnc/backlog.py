"""Backlog: plans decomposed into locked tasks (scrum for agents).

A plan is a set of BacklogTask entries sharing a plan_id. The driver loop
claims the next claimable task with a CAS update on the KV revision, so two
drivers racing on the same backlog entry: exactly one wins, the loser moves
on. This is the same optimistic-locking pattern as Bus.try_claim one level
above the mesh task queue.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from nats.js import JetStreamContext
from nats.js.errors import NotFoundError
from pydantic import BaseModel, Field

BACKLOG_BUCKET = "memnc-backlog"

# a claim older than this is considered abandoned (driver died mid-task)
CLAIM_STALE_S = 15 * 60

# terminal statuses
BACKLOG_DONE = "done"
BACKLOG_FAILED = "failed"
BACKLOG_TERMINAL = {BACKLOG_DONE, BACKLOG_FAILED}

MAX_ATTEMPTS = 3


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None


_TASK_LINE = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+")

def decompose_plan(text: str) -> list[str]:
    """Split a plan document into task prompts.

    Numbered (``1.``) or bullet (``- ``/``* ``) lines win when there are at
    least two of them -- that is the canonical shape of a written plan.
    Otherwise blank-line-separated paragraphs are the tasks, and a single
    block becomes one task.
    """
    stripped = text.strip()
    if not stripped:
        return []
    task_lines = [
        _TASK_LINE.sub("", line).strip()
        for line in stripped.splitlines()
        if _TASK_LINE.match(line)
    ]
    tasks = [line for line in task_lines if line]
    if len(tasks) >= 2:
        return tasks
    paragraphs = [
        block.strip()
        for block in re.split(r"\n\s*\n", stripped)
        if block.strip()
    ]
    if len(paragraphs) >= 2:
        return paragraphs
    return [stripped]

def _is_stale_claimed(task: BacklogTask) -> bool:
    claimed_at = _parse_ts(task.claimed_at)
    if claimed_at is None:
        return True  # unparseable timestamp: treat as abandoned
    age = datetime.now(UTC) - claimed_at
    return age.total_seconds() > CLAIM_STALE_S


class BacklogTask(BaseModel):
    """One work item derived from a plan, claimable by exactly one driver."""

    id: str = Field(default_factory=lambda: f"b-{uuid.uuid4().hex[:8]}")
    plan_id: str = ""
    title: str
    prompt: str = ""
    deps: list[str] = Field(default_factory=list)
    status: str = "planned"  # planned | claimed | done | failed
    claimant: str = ""
    claimed_at: str = ""
    attempts: int = 0
    mesh_task_id: str = ""
    # Weak->strong routing: the driver adds a strong-retry task (prefer_node
    # set) once this one exhausts its attempts on the weak pool.
    strong_retry: bool = False
    prefer_node: str = ""
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)


class BacklogStore:
    """KV-backed backlog with CAS claiming (the per-task lock)."""

    def __init__(self, js: JetStreamContext) -> None:
        self.js = js
        self._kv: Any = None

    async def _ensure_kv(self) -> Any:
        if self._kv is None:
            try:
                self._kv = await self.js.key_value(BACKLOG_BUCKET)
            except Exception:  # noqa: BLE001 - bucket absent on first use
                self._kv = await self.js.create_key_value(
                    bucket=BACKLOG_BUCKET, history=10
                )
        return self._kv

    async def add(
        self,
        title: str,
        prompt: str = "",
        plan_id: str = "",
        deps: list[str] | None = None,
        prefer_node: str = "",
        strong_retry: bool = False,
    ) -> BacklogTask:
        task = BacklogTask(
            title=title,
            prompt=prompt,
            plan_id=plan_id,
            deps=deps or [],
            prefer_node=prefer_node,
            strong_retry=strong_retry,
        )
        kv = await self._ensure_kv()
        await kv.put(task.id, task.model_dump_json().encode())
        return task

    async def get(self, backlog_id: str) -> BacklogTask | None:
        kv = await self._ensure_kv()
        try:
            entry = await kv.get(backlog_id)
        except (KeyError, NotFoundError):
            return None
        return BacklogTask.model_validate_json(entry.value)

    async def list(self) -> list[BacklogTask]:
        """All backlog entries, oldest created_at first."""
        kv = await self._ensure_kv()
        try:
            keys = await kv.keys()
        except NotFoundError:  # empty bucket: nats-py raises instead of []
            return []
        tasks: list[BacklogTask] = []
        for key in keys:
            try:
                entry = await kv.get(key)
                tasks.append(BacklogTask.model_validate_json(entry.value))
            except (KeyError, NotFoundError):  # noqa: PERF203 - deleted mid-scan
                continue
            except Exception:  # noqa: BLE001 - skip corrupt entries
                continue
        tasks.sort(key=lambda t: t.created_at)
        return tasks

    async def claim(self, backlog_id: str, claimant: str) -> BacklogTask | None:
        """CAS-claim: planned -> claimed. Conflict or gone => None."""
        kv = await self._ensure_kv()
        try:
            entry = await kv.get(backlog_id)
        except (KeyError, NotFoundError):
            return None
        task = BacklogTask.model_validate_json(entry.value)
        # planned = free; claimed = only stealable once the claim is stale
        if task.status != "planned" and not (task.status == "claimed" and _is_stale_claimed(task)):
            return None
        task.status = "claimed"
        task.claimant = claimant
        task.claimed_at = _now()
        try:
            await kv.update(backlog_id, task.model_dump_json().encode(), last=entry.revision)
        except Exception:  # noqa: BLE001 - CAS conflict: someone else claimed
            return None
        return task

    async def mark(
        self,
        backlog_id: str,
        status: str,
        mesh_task_id: str | None = None,
    ) -> BacklogTask | None:
        """Unconditional status update (the driver owns the claimed entry)."""
        kv = await self._ensure_kv()
        try:
            entry = await kv.get(backlog_id)
        except (KeyError, NotFoundError):
            return None
        task = BacklogTask.model_validate_json(entry.value)
        task.status = status
        task.updated_at = _now()
        if mesh_task_id is not None:
            task.mesh_task_id = mesh_task_id
        if status == BACKLOG_FAILED:
            task.attempts += 1
            # retry budget exhausted -> stay failed, otherwise back to planned
            if task.attempts < MAX_ATTEMPTS:
                task.status = "planned"
                task.claimant = ""
        await kv.put(backlog_id, task.model_dump_json().encode())
        return task

    async def next_claimable(self, claimant: str) -> BacklogTask | None:
        """First planned task whose deps are all done; claims it atomically."""
        tasks = await self.list()
        done_ids = {t.id for t in tasks if t.status == BACKLOG_DONE}
        for task in tasks:
            if task.status != "planned" and not (
                task.status == "claimed" and _is_stale_claimed(task)
            ):
                continue
            if any(dep not in done_ids for dep in task.deps):
                continue
            claimed = await self.claim(task.id, claimant)
            if claimed is not None:
                return claimed
        return None


def should_strong_retry(task: BacklogTask, strong_node: str) -> bool:
    """True when a terminally failed task deserves one strong-node retry.

    A task is terminal when its attempts are exhausted (status stays
    "failed"); only then, only when a strong node is configured, and only
    once per task (strong_retry flag guards against loops).
    """
    return (
        task.status == "failed"
        and task.attempts >= MAX_ATTEMPTS
        and not task.strong_retry
        and bool(strong_node)
    )


def make_strong_retry(task: BacklogTask, strong_node: str) -> BacklogTask:
    """Build the one-shot strong-node retry for a terminally failed task."""
    return BacklogTask(
        title=f"[strong-retry] {task.title}",
        prompt=task.prompt,
        plan_id=task.plan_id,
        deps=list(task.deps),
        strong_retry=True,
        prefer_node=strong_node,
    )
