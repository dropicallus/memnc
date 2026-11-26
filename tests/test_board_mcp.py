import pytest

from memnc import board
from memnc.mcp import board_server

FIXTURE = """# Roadmap

<!-- board:start -->
old block
<!-- board:end -->

## Contents

## Part I — Plan

## E1 Core

| ID | Task | Status | Evidence |
|---|---|---|---|
| T1.1 | Alpha | ☐ | **P0** · first |
| T1.2 | `Beta` | 🟡 | **P1** |
| T1.3 | Gamma | ✅ | done |

## Part II — Log

Log text that must never change.
"""


@pytest.fixture
def roadmap(tmp_path, monkeypatch):
    path = tmp_path / "roadmap.md"
    path.write_text(FIXTURE, encoding="utf-8")
    monkeypatch.setenv("BOARD_ROADMAP", str(path))
    monkeypatch.delenv("BOARD_WRITE", raising=False)
    return path


def test_list_returns_all_tasks_and_filters(roadmap):
    rows = board_server.board_list()
    assert [r["id"] for r in rows] == ["T1.1", "T1.2", "T1.3"]
    assert [r["id"] for r in board_server.board_list(priority="P0")] == ["T1.1"]
    assert [r["id"] for r in board_server.board_list(state="WORKING")] == ["T1.2"]


def test_list_rejects_unknown_filters(roadmap):
    assert "error" in board_server.board_list(priority="P9")[0]
    assert "error" in board_server.board_list(state="DONE")[0]


def test_get_reads_one_task(roadmap):
    task = board_server.board_get("T1.2")
    assert task["title"] == "Beta"
    assert task["state"] == "WORKING"
    assert task["priority"] == "P1"


def test_get_reports_unknown_and_malformed_ids(roadmap):
    assert "error" in board_server.board_get("T9.9")
    assert "error" in board_server.board_get("beta")


def test_set_state_is_refused_without_write_flag(roadmap):
    before = roadmap.read_text(encoding="utf-8")
    result = board_server.board_set_state("T1.1", "COMPLETED")
    assert "error" in result
    assert roadmap.read_text(encoding="utf-8") == before


def test_set_state_changes_only_the_row_and_regenerates_the_block(roadmap, monkeypatch):
    monkeypatch.setenv("BOARD_WRITE", "1")
    result = board_server.board_set_state("T1.1", "COMPLETED")
    assert result == {"id": "T1.1", "state": "COMPLETED"}
    text = roadmap.read_text(encoding="utf-8")
    assert "| T1.1 | Alpha | ✅ | **P0** · first |" in text
    assert "old block" not in text
    assert "Log text that must never change." in text
    assert board.updated(text) == text  # the block is current after the write


def test_set_state_rejects_invalid_input_without_writing(roadmap, monkeypatch):
    monkeypatch.setenv("BOARD_WRITE", "1")
    before = roadmap.read_text(encoding="utf-8")
    assert "error" in board_server.board_set_state("T1.1", "DONE")
    assert "error" in board_server.board_set_state("bad id", "WORKING")
    assert "error" in board_server.board_set_state("T9.9", "WORKING")
    assert roadmap.read_text(encoding="utf-8") == before


def test_set_state_only_touches_part_one(roadmap, monkeypatch):
    monkeypatch.setenv("BOARD_WRITE", "1")
    board_server.board_set_state("T1.2", "CANCELED")
    text = roadmap.read_text(encoding="utf-8")
    assert text.split("## Part II")[1] == FIXTURE.split("## Part II")[1]


def test_server_builds_with_three_tools():
    server = board_server.create_server()
    assert server is not None
