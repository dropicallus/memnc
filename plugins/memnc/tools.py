"""Tool handlers — run the meshctl CLI and return JSON strings.

The plugin shells out to ``meshctl`` (installed with the memnc package)
instead of importing memnc internals, so it works against any running
mesh regardless of the local package version.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

_MESHCTL = os.environ.get("MEMNC_MESHCTL", "meshctl")
_TIMEOUT = int(os.environ.get("MEMNC_PLUGIN_TIMEOUT", "900"))


def _run(*cli_args: str) -> str:
    """Run meshctl and return its stdout, or an error JSON string."""
    exe = shutil.which(_MESHCTL)
    if exe is None:
        return json.dumps({
            "error": (
                f"meshctl not found ({_MESHCTL!r}). Install the memnc package "
                "or point MEMNC_MESHCTL at the binary."
            )
        })
    try:
        proc = subprocess.run(
            [exe, *cli_args],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return json.dumps({"error": f"meshctl {' '.join(cli_args[:2])} timed out after {_TIMEOUT}s"})
    except OSError as exc:
        return json.dumps({"error": f"meshctl failed to start: {exc}"})
    out = proc.stdout.strip()
    if proc.returncode != 0:
        err = (proc.stderr.strip() or out or f"exit code {proc.returncode}")
        return json.dumps({"error": err[:2000]})
    return json.dumps({"ok": True, "output": out[:20000]})


def mesh_submit(args: dict, **kwargs) -> str:
    """Submit a task to the mesh."""
    task = str(args.get("task", "")).strip()
    if not task:
        return json.dumps({"error": "No task provided"})
    cmd = ["submit", task]
    if args.get("watch", True):
        cmd.append("--watch")
    if args.get("node"):
        cmd += ["--node", str(args["node"])]
    if args.get("needs_model"):
        cmd += ["--needs-model", str(args["needs_model"])]
    if args.get("needs_role"):
        cmd += ["--needs-role", str(args["needs_role"])]
    return _run(*cmd)


def mesh_status(args: dict, **kwargs) -> str:
    """Show roster and running tasks."""
    return _run("roster")


def mesh_state(args: dict, **kwargs) -> str:
    """Show the recorded state of one task."""
    task_id = str(args.get("task_id", "")).strip()
    if not task_id:
        return json.dumps({"error": "No task_id provided"})
    return _run("state", task_id)


def mesh_tail(args: dict, **kwargs) -> str:
    """Tail the live events of one task."""
    task_id = str(args.get("task_id", "")).strip()
    if not task_id:
        return json.dumps({"error": "No task_id provided"})
    return _run("tail", task_id)
