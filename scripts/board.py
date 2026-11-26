"""Regenerate the board block at the top of docs/roadmap.md (see memnc.board).

    python scripts/board.py          rewrite the block between the markers
    python scripts/board.py --check  exit 1 if the block is out of date
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from memnc.board import ROADMAP, tasks, updated  # noqa: E402

__all__ = ["ROADMAP", "tasks", "updated", "main"]


def main(argv: list[str]) -> int:
    text = ROADMAP.read_text(encoding="utf-8")
    new = updated(text)
    if "--check" in argv:
        if new != text:
            print("docs/roadmap.md: board block is out of date; run scripts/board.py", file=sys.stderr)
            return 1
        return 0
    if new != text:
        ROADMAP.write_text(new, encoding="utf-8")
        print("board block updated")
    else:
        print("board block already up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
