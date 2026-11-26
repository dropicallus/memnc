"""MCP stdio server for the roadmap board: list and read tasks, and change a state on opt-in.

Reads are always on. A state change rewrites the roadmap row and regenerates the board,
so it runs only when the operator starts the server with BOARD_WRITE=1. The roadmap file
is BOARD_ROADMAP when set, otherwise docs/roadmap.md in the repository.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from ..board import PRIORITIES, ROADMAP, STATES, get_task, set_state, tasks, updated
from ..fsutil import atomic_write

TASK_ID = re.compile(r"^T\d+\.\d+$")


def roadmap_path() -> Path:
    return Path(os.environ.get("BOARD_ROADMAP", str(ROADMAP)))


def board_list(priority: str = "", state: str = "") -> list[dict]:
    """List tasks on the roadmap board. Optional filters: priority P0, P1 or P2; state SUBMITTED, WORKING, COMPLETED, CANCELED or DEFERRED."""
    if priority and priority not in PRIORITIES:
        return [{"error": f"priority must be one of {', '.join(PRIORITIES)}"}]
    if state and state not in STATES:
        return [{"error": f"state must be one of {', '.join(STATES)}"}]
    text = roadmap_path().read_text(encoding="utf-8")
    out = []
    for task_id, title, task_state, task_priority in tasks(text):
        if priority and task_priority != priority:
            continue
        if state and task_state != state:
            continue
        out.append({"id": task_id, "title": title, "state": task_state, "priority": task_priority})
    return out


def board_get(task_id: str) -> dict:
    """Read one task: id, title, state, priority and evidence. Task ids look like T11.20."""
    if not TASK_ID.match(task_id):
        return {"error": "task_id must look like T11.20"}
    text = roadmap_path().read_text(encoding="utf-8")
    try:
        return get_task(text, task_id)
    except KeyError:
        return {"error": f"no task {task_id} on the board"}


def board_set_state(task_id: str, state: str) -> dict:
    """Change a task's state to SUBMITTED, WORKING, COMPLETED, CANCELED or DEFERRED and regenerate the board. Works only when the server runs with BOARD_WRITE=1."""
    if os.environ.get("BOARD_WRITE") != "1":
        return {"error": "writes are disabled; the operator must start the server with BOARD_WRITE=1"}
    if not TASK_ID.match(task_id):
        return {"error": "task_id must look like T11.20"}
    if state not in STATES:
        return {"error": f"state must be one of {', '.join(STATES)}"}
    path = roadmap_path()
    try:
        new_text = set_state(path.read_text(encoding="utf-8"), task_id, state)
    except KeyError:
        return {"error": f"no task {task_id} on the board"}
    atomic_write(path, updated(new_text))
    return {"id": task_id, "state": state}


def create_server() -> MCPServer:
    server = MCPServer("memnc-board")
    server.tool()(board_list)
    server.tool()(board_get)
    server.tool()(board_set_state)
    return server


def main() -> None:
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
