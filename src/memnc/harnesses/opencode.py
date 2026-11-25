"""OpenCode harness: runs `opencode run` headlessly against a workspace.

Proven workflow (2026-11-15, node-c/node-c against local ollama):
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
import json
import os
import re
import shutil
import signal
from pathlib import Path

from . import HarnessCaps, HarnessResult, register
from .workspace import compare_trees, snapshot_tree, workspace_diff

DEFAULT_TIMEOUT_S = 900

# Lines in `opencode run` output that indicate one tool execution.
TOOL_MARKERS = ("←", "→", "Edit(", "Read(", "Bash(")


def _is_tool_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith(TOOL_MARKERS) or stripped.startswith("$ ")


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _stderr_error(text: str) -> str | None:
    """Return an error line from opencode's stderr, or None.

    opencode streams tool output to stderr too (a coder that runs
    ``print("hello")`` puts ``hello`` there), so stderr alone is NOT failure.
    Real failures appear as ``Error: {...}`` / ``UnknownError`` lines and
    always come with a non-zero exit code.
    """
    for line in reversed([ln.strip() for ln in text.splitlines() if ln.strip()]):
        low = line.lower()
        if low.startswith("error") or "unknown error" in low or '"error"' in low:
            return line
    return None


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


def stage_config() -> tuple[str, dict[str, str]]:
    """Copy the read-only node config into a writable dir for this run.

    opencode 1.19 writes a ``.gitignore`` next to its config on startup, so a
    read-only bind mount of the node's config dir aborts every run with
    "FileSystem.writeFile". The host file stays the source of truth; each run
    copies it into the tmpfs and points the child at that copy.

    Returns ``(config_path, env_overlay)``; both empty when the node ships no
    config, in which case opencode uses its own defaults.
    """
    source = Path(os.environ.get("OPENCODE_CONFIG_SOURCE", "/opt/opencode-config"))
    if not source.is_dir() or not (source / "opencode.json").is_file():
        return "", {}
    home = Path(os.environ.get("OPENCODE_HOME", "/tmp/.opencode-home"))
    target = home / ".config/opencode"
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source / "opencode.json", target / "opencode.json")
    config_path = str(target / "opencode.json")
    return config_path, {
        "XDG_CONFIG_HOME": str(target.parent),
        "OPENCODE_CONFIG": config_path,
    }


def _maybe_clean_state() -> None:
    """Opt-in stale-state wipe (opencode hang workaround, see docstring)."""
    if os.environ.get("OPENCODE_CLEAN_STATE") == "1":
        state = Path.home() / ".local/share/opencode"
        shutil.rmtree(state, ignore_errors=True)


_KNOWN_PROVIDER_PREFIXES = (
    "ollama/",
    "openai/",
    "anthropic/",
    "google/",
    "openrouter/",
    "azure/",
    "github-copilot/",
    "opencode/",
    "mistral/",
    "groq/",
    "xai/",
    "amazon-bedrock/",
    "github/",
)


def _configured_providers() -> tuple[str, ...]:
    """Provider ids defined by the node's opencode config (cached by mtime).

    Adding a provider to opencode.json alone must be enough to use
    ``--model <provider>/<id>``; an unknown prefix would otherwise be
    mistaken for an ollama tag and silently rewritten to ``ollama/<p>/<id>``.
    """
    global _PROVIDER_CACHE
    src = Path(os.environ.get("OPENCODE_CONFIG_SOURCE", "/opt/opencode-config"))
    path = src / "opencode.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return ()
    if _PROVIDER_CACHE and _PROVIDER_CACHE[0] == mtime:
        return _PROVIDER_CACHE[1]
    try:
        data = json.loads(path.read_text())
        names = tuple(data.get("provider", {}).keys())
    except (OSError, ValueError, AttributeError):
        names = ()
    _PROVIDER_CACHE = (mtime, names)
    return names


_PROVIDER_CACHE: tuple[float, tuple[str, ...]] | None = None


def qualify_model(model: str) -> str:
    """Prefix a bare model name with the ``ollama/`` provider id.

    Workers carry ONE model setting per role (MODEL_CODER) and feed it to both
    the inproc path (needs the bare ollama tag) and this harness (opencode
    resolves `--model provider/id` against the provider config, a bare tag
    would be read as provider/model). So the harness adds the ``ollama/``
    prefix itself unless the string is already provider-qualified or empty.
    """
    if not model:
        return model
    head, sep, _ = model.partition("/")
    if sep and (model.startswith(_KNOWN_PROVIDER_PREFIXES) or head in _configured_providers()):
        return model
    if not sep:
        return f"ollama/{model}"
    # Unqualified name with a slash: an ollama namespace tag (e.g.
    # "mannix/qwen3.6-27b...") unless a provider of that name exists.
    return f"ollama/{model}"


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
        self.model = qualify_model(model)
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
        _, env_overlay = stage_config()
        before = snapshot_tree(workspace)
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
                env=os.environ | env_overlay,
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
        stderr_text = _ANSI_RE.sub("", stderr.decode(errors="replace"))
        error = _stderr_error(stderr_text)
        diff = await workspace_diff(workspace)
        files = compare_trees(workspace, before)
        ok = proc.returncode == 0 and not error
        return HarnessResult(
            ok=ok,
            summary=parsed["summary"],
            diff=diff,
            tool_events=parsed["tool_events"],
            exit_code=proc.returncode,
            error=error,
            files=files,
        )
