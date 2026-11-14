"""Pi harness: runs `pi -p --mode json` headlessly against a workspace.

Proven workflow (2026-11-14, weak->strong loop on the memnc repo):
- pi edits files directly in the workspace (git worktree recommended); the
  product is the workspace diff, not structured patches.
- 7b-class models emit native tool_calls (6+ tool executions per run); the
  32b coder does NOT (ollama template has no tools section -> pi sees 0 tool
  events). ``tool_events == 0`` is therefore a routing signal.
- 7b can emit 5 parallel tool_calls -> empty assistant turn -> pi retry loop;
  allow generous timeouts for weak models.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
from pathlib import Path

from . import HarnessCaps, HarnessResult, register

DEFAULT_TIMEOUT_S = 900


def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL the harness process group; fall back to the process itself."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def parse_events(text: str) -> dict:
    """Parse a pi --mode json event stream (JSONL) into summary fields.

    Tolerant by design: pi writes huge message_update lines, error turns and
    (with broken providers) garbage; unknown or malformed lines are skipped.
    """
    tool_events = 0
    summary = ""
    settled = False
    errors: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        etype = event.get("type")
        if etype == "tool_execution_start":
            tool_events += 1
        elif etype == "agent_settled":
            settled = True
        elif etype == "message_end":
            msg = event.get("message") or {}
            if msg.get("role") != "assistant":
                continue
            parts = msg.get("content") or []
            texts = [
                p.get("text", "")
                for p in parts
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            joined = "\n".join(t for t in texts if t)
            if joined:
                summary = joined
            if msg.get("errorMessage"):
                errors.append(str(msg["errorMessage"]))
            elif msg.get("stopReason") not in (None, "stop", "toolUse"):
                errors.append(f"stopReason={msg.get('stopReason')}")
    return {
        "summary": summary,
        "tool_events": tool_events,
        "settled": settled,
        "errors": errors,
    }


@register
class PiHarness:
    """External-process harness around the pi coding agent CLI."""

    name = "pi"

    def __init__(
        self,
        model: str,
        pi_bin: str | None = None,
        extra_args: list[str] | None = None,
    ):
        self.model = model
        self.pi_bin = pi_bin or shutil.which("pi") or "pi"
        self.extra_args = list(extra_args or [])

    def capabilities(self) -> HarnessCaps:
        return HarnessCaps(native_tools=True, external_process=True)

    async def run(
        self, prompt: str, workspace: Path, *, timeout_s: int = DEFAULT_TIMEOUT_S
    ) -> HarnessResult:
        workspace = Path(workspace)
        workspace.mkdir(parents=True, exist_ok=True)
        cmd = [
            self.pi_bin,
            "-p",
            "--mode",
            "json",
            "--model",
            self.model,
            *self.extra_args,
            prompt,
        ]
        try:
            # Own process group: on timeout the whole tree (pi + children)
            # must die, or an orphaned child keeps our stdout pipe open.
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except (FileNotFoundError, PermissionError) as exc:
            return HarnessResult(ok=False, error=f"cannot start pi: {exc}")

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except TimeoutError:
            _kill_tree(proc)
            await proc.wait()
            return HarnessResult(
                ok=False,
                error=f"pi timed out after {timeout_s}s",
                transcript_path=str(workspace),
            )

        parsed = parse_events(stdout.decode(errors="replace"))
        stderr_tail = stderr.decode(errors="replace").strip().splitlines()
        error = "; ".join(parsed["errors"]) or (stderr_tail[-1] if stderr_tail else None)
        diff = await self._git_diff(workspace)
        ok = proc.returncode == 0 and parsed["settled"] and not parsed["errors"]
        return HarnessResult(
            ok=ok,
            summary=parsed["summary"],
            diff=diff,
            tool_events=parsed["tool_events"],
            exit_code=proc.returncode,
            error=error,
        )

    @staticmethod
    async def _git_diff(workspace: Path) -> str:
        """Workspace diff including untracked files.

        ``git add -N .`` marks untracked files intent-to-add so plain
        ``git diff`` shows their content; it stages nothing.
        """
        try:
            add = await asyncio.create_subprocess_exec(
                "git", "add", "-N", ".",
                cwd=str(workspace),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(add.communicate(), timeout=30)
            proc = await asyncio.create_subprocess_exec(
                "git",
                "diff",
                cwd=str(workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            return stdout.decode(errors="replace")
        except (TimeoutError, FileNotFoundError, OSError):
            return ""
