import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "board.py"


def _board_module():
    spec = importlib.util.spec_from_file_location("board", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_board_block_is_up_to_date():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_every_open_task_has_a_priority():
    board = _board_module()
    text = (ROOT / "docs" / "roadmap.md").read_text(encoding="utf-8")
    unranked = [
        task_id
        for task_id, _title, state, prio in board.tasks(text)
        if state in {"SUBMITTED", "WORKING"} and not prio
    ]
    assert unranked == []
