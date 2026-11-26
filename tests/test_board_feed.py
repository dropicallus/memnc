"""Board feeder: reconcile mesh results onto the board and fill free lanes."""

from __future__ import annotations

import asyncio

import pytest

from memnc.board_feed import (
    BoardFeeder,
    FeedStore,
    InProcessBoard,
    board_tasks,
    open_tasks,
    task_text,
)
from memnc.schema import TaskSnapshot, TaskState

FIXTURE = """# Roadmap

<!-- board:start -->
old block
<!-- board:end -->

## Part I — Plan

## E1 Core

| ID | Task | Status | Evidence |
|---|---|---|---|
| T1.1 | Alpha | ☐ | **P0** · first |
| T1.2 | `Beta` | ☐ | **P1** · second |
| T1.3 | Gamma | ✅ | done |
| T1.4 | Delta | ☐ | no priority |

## Part II — Log

Log text that must never change.
"""


class FakeBoard:
    """BoardSource over an in-memory row list; records every write."""

    def __init__(self, rows):
        self.rows = [dict(row) for row in rows]
        self.writes: list[tuple[str, str]] = []
        self.fail_writes = False

    def _row(self, task_id):
        for row in self.rows:
            if row["id"] == task_id:
                return row
        raise KeyError(task_id)

    async def list(self, state: str = "") -> list[dict]:
        return [dict(row) for row in self.rows if not state or row["state"] == state]

    async def get(self, task_id: str) -> dict:
        return dict(self._row(task_id))

    async def set_state(self, task_id: str, state: str) -> dict:
        if self.fail_writes:
            return {"error": "writes are disabled"}
        self._row(task_id)["state"] = state
        self.writes.append((task_id, state))
        return {"id": task_id, "state": state}


class FakeMesh:
    """submit + snapshot hooks; snapshot returns whatever was scripted."""

    def __init__(self):
        self.submitted: list[str] = []
        self.snapshots: dict[str, TaskSnapshot] = {}
        self.fail_submit = False
        self.counter = 0

    async def submit(self, text: str) -> str:
        if self.fail_submit:
            raise RuntimeError("nats down")
        self.counter += 1
        task_id = f"task-{self.counter}"
        self.submitted.append(text)
        return task_id

    async def snapshot(self, task_id: str) -> TaskSnapshot | None:
        return self.snapshots.get(task_id)


def rows(*specs):
    return [{"id": i, "title": t, "state": s, "priority": p} for i, t, s, p in specs]


def feeder(tmp_path, board, mesh, capacity=1, max_attempts=3):
    return BoardFeeder(
        board=board,
        submit=mesh.submit,
        snapshot=mesh.snapshot,
        store=FeedStore(tmp_path / "feed.json"),
        capacity=capacity,
        max_attempts=max_attempts,
    )


def snap(task_id, state, **fields):
    return TaskSnapshot(task_id=task_id, trace_id="tr", task="t", state=state, **fields)


def test_open_tasks_filters_and_orders_by_priority():
    board = rows(
        ("T2", "no prio", "SUBMITTED", ""),
        ("T1", "later", "SUBMITTED", "P2"),
        ("T3", "first", "SUBMITTED", "P0"),
        ("T4", "done", "COMPLETED", "P0"),
    )
    assert [task.id for task in open_tasks(board)] == ["T3", "T1", "T2"]


def test_board_tasks_ignores_error_rows():
    assert board_tasks([{"error": "nope"}, {"id": "T1", "title": "a", "state": "SUBMITTED"}]) == [
        board_tasks([{"id": "T1", "title": "a", "state": "SUBMITTED"}])[0]
    ]


def test_task_text_names_the_board_id_and_forbids_self_editing():
    text = task_text(
        board_tasks([{"id": "T1.1", "title": "Alpha", "state": "SUBMITTED", "priority": "P0"}])[0]
    )
    assert "[Board T1.1] Alpha" in text
    assert "Do not edit the board" in text
    assert "P0" in text


def test_tick_starts_one_task_and_marks_it_working(tmp_path):
    board = FakeBoard(rows(("T1", "a", "SUBMITTED", "P0")))
    mesh = FakeMesh()
    feed = feeder(tmp_path, board, mesh)

    result = asyncio.run(feed.tick())

    assert result.started == ["T1"]
    assert board.writes == [("T1", "WORKING")]
    assert mesh.submitted and "[Board T1]" in mesh.submitted[0]


def test_capacity_blocks_second_task_until_the_first_finishes(tmp_path):
    board = FakeBoard(rows(("T1", "a", "SUBMITTED", "P0"), ("T2", "b", "SUBMITTED", "P1")))
    mesh = FakeMesh()
    feed = feeder(tmp_path, board, mesh, capacity=1)

    first = asyncio.run(feed.tick())
    assert first.started == ["T1"]

    mesh.snapshots["task-1"] = snap("task-1", TaskState.CODING)
    second = asyncio.run(feed.tick())
    assert second.started == []
    assert [entry["board"] for entry in second.running] == ["T1"]

    mesh.snapshots["task-1"] = snap("task-1", TaskState.DONE)
    third = asyncio.run(feed.tick())
    assert third.completed == ["T1"]
    assert third.started == ["T2"]
    assert ("T1", "COMPLETED") in board.writes


def test_state_survives_a_restart(tmp_path):
    board = FakeBoard(rows(("T1", "a", "SUBMITTED", "P0")))
    mesh = FakeMesh()
    asyncio.run(feeder(tmp_path, board, mesh).tick())

    again = feeder(tmp_path, board, mesh)
    mesh.snapshots["task-1"] = snap("task-1", TaskState.CODING)
    result = asyncio.run(again.tick())

    assert result.started == []  # still tracked, not submitted twice
    assert len(mesh.submitted) == 1


def test_failed_task_goes_back_to_the_board(tmp_path):
    board = FakeBoard(rows(("T1", "a", "SUBMITTED", "P0")))
    mesh = FakeMesh()
    feed = feeder(tmp_path, board, mesh, capacity=1)
    asyncio.run(feed.tick())

    mesh.snapshots["task-1"] = snap("task-1", TaskState.FAILED)
    result = asyncio.run(feed.tick())

    assert result.requeued == ["T1"]
    assert board.rows[0]["state"] == "SUBMITTED"


def test_task_is_deferred_after_max_attempts(tmp_path):
    board = FakeBoard(rows(("T1", "a", "SUBMITTED", "P0")))
    mesh = FakeMesh()
    feed = feeder(tmp_path, board, mesh, max_attempts=2)

    result = None
    for attempt in range(2):
        asyncio.run(feed.tick())
        mesh.snapshots[f"task-{attempt + 1}"] = snap(f"task-{attempt + 1}", TaskState.FAILED)
        result = asyncio.run(feed.tick())

    assert result is not None
    assert result.deferred == ["T1"]
    assert board.rows[0]["state"] == "DEFERRED"


def test_a_task_lost_from_the_ledger_is_requeued(tmp_path):
    board = FakeBoard(rows(("T1", "a", "SUBMITTED", "P0")))
    mesh = FakeMesh()
    feed = feeder(tmp_path, board, mesh)
    asyncio.run(feed.tick())

    result = asyncio.run(feed.tick())  # snapshot() -> None

    assert result.requeued == ["T1"]
    assert board.rows[0]["state"] == "SUBMITTED"


def test_a_row_moved_by_a_human_is_not_touched(tmp_path):
    board = FakeBoard(rows(("T1", "a", "SUBMITTED", "P0")))
    mesh = FakeMesh()
    feed = feeder(tmp_path, board, mesh)
    asyncio.run(feed.tick())
    board.rows[0]["state"] = "CANCELED"
    mesh.snapshots["task-1"] = snap("task-1", TaskState.DONE)

    result = asyncio.run(feed.tick())

    assert result.completed == []
    assert board.writes == [("T1", "WORKING")]


def test_submit_failure_keeps_the_row_open_and_adds_no_slot(tmp_path):
    board = FakeBoard(rows(("T1", "a", "SUBMITTED", "P0"), ("T2", "b", "SUBMITTED", "P1")))
    mesh = FakeMesh()
    mesh.fail_submit = True
    feed = feeder(tmp_path, board, mesh, capacity=1)

    result = asyncio.run(feed.tick())

    assert result.started == []
    assert len(result.errors) == 1
    assert board.writes == []
    assert FeedStore(tmp_path / "feed.json").load()["in_flight"] == {}


def test_board_write_refusal_is_reported_but_the_task_stays_tracked(tmp_path):
    board = FakeBoard(rows(("T1", "a", "SUBMITTED", "P0")))
    mesh = FakeMesh()
    board.fail_writes = True
    feed = feeder(tmp_path, board, mesh)

    result = asyncio.run(feed.tick())

    assert result.started == ["T1"]
    assert result.errors
    assert FLIGHT(tmp_path) == {"T1": "task-1"}


def FLIGHT(tmp_path):
    data = FeedStore(tmp_path / "feed.json").load()["in_flight"]
    return {key: value["task_id"] for key, value in data.items()}


@pytest.mark.parametrize("mode", ["pull", "push"])
def test_mode_is_validated_not_silently_ignored(tmp_path, mode):
    feed = feeder(tmp_path, FakeBoard([]), FakeMesh())
    feed.mode = mode
    assert asyncio.run(feed.tick()).started == []
    with pytest.raises(ValueError):
        BoardFeeder(
            board=FakeBoard([]),
            submit=FakeMesh().submit,
            snapshot=FakeMesh().snapshot,
            store=FeedStore(tmp_path / "x.json"),
            mode="teleport",
        )


def test_in_process_board_rewrites_only_the_row(tmp_path):
    roadmap = tmp_path / "roadmap.md"
    roadmap.write_text(FIXTURE, encoding="utf-8")
    board = InProcessBoard(roadmap)

    asyncio.run(board.set_state("T1.2", "WORKING"))

    text = roadmap.read_text(encoding="utf-8")
    assert "| T1.2 | `Beta` | 🟡 | **P1** · second |" in text
    assert "| T1.1 | Alpha | ☐ | **P0** · first |" in text
    assert text.split("## Part II")[1] == FIXTURE.split("## Part II")[1]
    assert "old block" not in text
    listed = asyncio.run(board.list(state="WORKING"))
    assert [row["id"] for row in listed] == ["T1.2"]
    assert asyncio.run(board.get("T1.1"))["priority"] == "P0"


def test_in_process_board_can_be_read_only(tmp_path):
    roadmap = tmp_path / "roadmap.md"
    roadmap.write_text(FIXTURE, encoding="utf-8")
    board = InProcessBoard(roadmap, write=False)

    assert "error" in asyncio.run(board.set_state("T1.1", "WORKING"))
    assert roadmap.read_text(encoding="utf-8") == FIXTURE
