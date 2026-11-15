"""OpenCode harness: runs `opencode run` headlessly against a workspace.

Proven workflow (2026-11-15, node-c/maco against local ollama):
- opencode 1.19.5 + `mannix/qwen3.6-27b-a3b-coder:CD-IQ4_K_M` emits native
  tool_calls via the ollama /v1 template and completes a full create-file +
  run + confirm loop (~3-4 min incl. model load).
- Unlike pi, `opencode run` has no --mode json: stdout is a human-readable
  stream. parse_output() therefore uses tolerant tool-marker heuristics
  ("←"/"→" tool arrows, "$ " shell lines); settled == exit 0.
- opencode reuses ~/.local/share/opencode state; stale state can HANG the run
  (observed on first smoke). The harness never cleans user state by default;
  set OPENCODE_CLEAN_STATE=1 in the worker env to wipe it before each run.
- The model string must be provider-qualified (e.g. "ollama/mannix/...").
  A bare ollama name falls back to opencode's own default provider config
  (~/.config/opencode/opencode.json) — keep provider/model ids intact.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
from pathlib import Path

from . import HarnessCaps, HarnessResult, register
from .workspace import workspace_diff

DEFAULT_TIMEOUT_S = 900

# Lines in `opencode run` output that indicate one tool execution.
TOOL_MARKERS = ("←", "→", "Edit(", "Read(", "Bash(")


def _is_tool_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith(TOOL_MARKERS) or stripped.startswith("$ ")


def parse_output(text: str) -> dict:
    """Parse an `opencode run` stdout stream into summary fields.

    Tolerant by design: no structured format exists, so unknown lines are
    ignored and the summary is the trailing block of plain text lines.
    """
    tool_events = 0
    plain: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if _is_tool_line(line):
            tool_events += 1
        else:
            plain.append(line.strip())
    # The build header ("build · <model>") and noise lines are skipped by
    # taking only the final contiguous-ish run of short plain lines.
    tail = plain[-5:] if len(plain) > 5 else plain
    summary = "\n".join(tail)
    return {"summary": summary, "tool_events": tool_events}


def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL the harness process group; fall back to the process itself."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def _maybe_clean_state() -> None:
    """Opt-in stale-state wipe (opencode hang workaround, see docstring)."""
    if os.environ.get("OPENCODE_CLEAN_STATE") == "1":
        state = Path.home() / ".local/share/opencode"
        shutil.rmtree(state, ignore_errors=True)


@register
class OpenCodeHarness:
    """External-process harness around the opencode coding agent CLI."""

    name = "opencode"

    def __init__(
        self,
        model: str,
        opencode_bin: str | None = None,
        extra_args: list[str] | None = None,
    ):
        self.model = model
        self.opencode_bin = opencode_bin or shutil.which("opencode") or "opencode"
        self.extra_args = list(extra_args or [])

    def capabilities(self) -> HarnessCaps:
        return HarnessCaps(native_tools=True, external_process=True)

    async def run(
        self, prompt: str, workspace: Path, *, timeout_s: int = DEFAULT_TIMEOUT_S
    ) -> HarnessResult:
        workspace = Path(workspace)
        workspace.mkdir(parents=True, exist_ok=True)
        _maybe_clean_state()
        cmd = [
            self.opencode_bin,
            "run",
            prompt,
            "--model",
            self.model,
            *self.extra_args,
        ]
        try:
            # Own process group: on timeout the whole tree (opencode + shell
            # children) must die, or an orphaned child keeps our stdout pipe open.
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except (FileNotFoundError, PermissionError) as exc:
            return HarnessResult(ok=False, error=f"cannot start opencode: {exc}")

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except TimeoutError:
            _kill_tree(proc)
            await proc.wait()
            return HarnessResult(
                ok=False,
                error=f"opencode timed out after {timeout_s}s",
                transcript_path=str(workspace),
            )

        parsed = parse_output(stdout.decode(errors="replace"))
        stderr_tail = stderr.decode(errors="replace").strip().splitlines()
        error = stderr_tail[-1] if stderr_tail else None
        diff = await workspace_diff(workspace)
        ok = proc.returncode == 0 and not error
        return HarnessResult(
            ok=ok,
            summary=parsed["summary"],
            diff=diff,
            tool_events=parsed["tool_events"],
            exit_code=proc.returncode,
            error=error,
        )
