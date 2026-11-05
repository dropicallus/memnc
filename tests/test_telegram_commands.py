"""Pure-logic tests for the improved Telegram command formatting."""

from memnc.telegram import format_roster, format_task, summarize_tasks


def snap(task_id: str, state: str, claimed_by: str = "", task: str = "do the thing") -> dict:
    return {
        "task_id": task_id,
        "state": state,
        "claimed_by": claimed_by,
        "task": task,
    }


def test_summary_header_counts_by_state():
    snaps = [snap(f"id{i}", "FAILED") for i in range(3)] + [snap("x", "DONE")]
    out = summarize_tasks(snaps)
    assert out.startswith("4 tasks: FAILED=3 DONE=1")


def test_summary_shows_active_first_then_recent():
    snaps = [snap(f"old{i}", "DONE") for i in range(20)] + [snap("new", "CRITIQUE", "node-a-critic:critic")]
    out = summarize_tasks(snaps, limit=5)
    lines = out.splitlines()
    # active task appears before any terminal one
    idx_active = next(i for i, l in enumerate(lines) if "#new" in l)
    idx_done = min(i for i, l in enumerate(lines) if "#old" in l)
    assert idx_active < idx_done
    assert len([l for l in lines if l.startswith("#")]) == 6  # 1 active + 5 recent


def test_summary_filter_state():
    snaps = [snap("a", "FAILED"), snap("b", "DONE")]
    out = summarize_tasks(snaps, "failed")
    assert "#a FAILED" in out and "b" not in out.split("showing")[1]


def test_summary_filter_no_match():
    snaps = [snap("a", "DONE")]
    assert "no tasks with state: failed" in summarize_tasks(snaps, "failed")


def test_summary_includes_task_title():
    out = summarize_tasks([snap("a", "DONE", task="print T6-GATE-OK")])
    assert "print T6-GATE-OK" in out


def test_summary_empty():
    assert summarize_tasks([]) == "no tasks in ledger"


def test_format_roster_lines():
    nodes = [
        {
            "node_id": "node-a-coder",
            "roles": ["coder"],
            "models": ["llama3.2:3b"],
            "vram_gb": 16.0,
            "gpu": "rx 6800 xt",
        }
    ]
    out = format_roster(nodes)
    assert "1 nodes:" in out
    assert "node-a-coder roles=coder models=llama3.2:3b vram=16GB gpu=rx 6800 xt" in out


def test_format_roster_empty():
    assert format_roster([]) == "no nodes in roster"


def test_format_task_shows_title():
    out = format_task(snap("abc123", "DONE", claimed_by="node-a-critic:critic"))
    assert "#abc123 DONE" in out
    assert "do the thing" in out
