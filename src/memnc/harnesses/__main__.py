"""Probe CLI: run one prompt through a registered harness from the host.

    python -m memnc.harnesses --workspace /path/to/wt \
        --model ollama-power/qwen2.5:7b --timeout 900 "implement X"

Prints a JSON result object; exit 0 iff the harness reports ok.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from . import get_harness, known_harnesses


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m memnc.harnesses",
        description="Run one prompt through a harness against a workspace.",
    )
    parser.add_argument("prompt")
    parser.add_argument("--harness", default="pi")
    parser.add_argument("--model", required=True)
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--diff-out", default=None, help="also write the git diff here")
    args = parser.parse_args(argv)

    if args.harness not in known_harnesses():
        parser.error(f"unknown harness {args.harness!r}; known: {known_harnesses()}")

    harness = get_harness(args.harness, model=args.model)
    workspace = Path(args.workspace).expanduser().resolve()
    result = asyncio.run(
        harness.run(args.prompt, workspace, timeout_s=args.timeout)
    )
    if args.diff_out and result.diff:
        Path(args.diff_out).expanduser().write_text(result.diff)
    print(
        json.dumps(
            {
                "ok": result.ok,
                "exit_code": result.exit_code,
                "tool_events": result.tool_events,
                "summary": result.summary,
                "error": result.error,
                "diff_lines": result.diff.count("\n"),
            },
            indent=2,
        )
    )
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
