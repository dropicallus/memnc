"""Shell tool implementation. Full shell by policy; container is the sandbox."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from pathlib import Path

from ..fsutil import PathEscapeError, safe_path

log = logging.getLogger(__name__)


async def run_shell(
    root: Path,
    cmd: str,
    *,
    cwd: str = ".",
    timeout_s: int = 120,
    max_output_bytes: int = 65_536,
    allow_shell: bool = True,
) -> str:
    if not allow_shell:
        return json.dumps({"ok": False, "error": "shell disabled by configuration"})

    started = time.monotonic()
    try:
        workdir = safe_path(root, cwd)
        if not workdir.is_dir():
            workdir = Path(root).resolve()
        timeout = min(timeout_s if timeout_s > 0 else 120, 3600)

        process = await asyncio.create_subprocess_shell(
            cmd,
            cwd=str(workdir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        timed_out = False
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = await process.communicate()

        def clip(data: bytes) -> tuple[str, bool]:
            truncated = len(data) > max_output_bytes
            return data[:max_output_bytes].decode("utf-8", errors="replace"), truncated

        out, out_truncated = clip(stdout or b"")
        err, err_truncated = clip(stderr or b"")
        duration_ms = int((time.monotonic() - started) * 1000)
        log.info(
            "shell",
            extra={
                "cmd": cmd[:200],
                "cwd": str(workdir),
                "exit_code": process.returncode,
                "duration_ms": duration_ms,
                "timed_out": timed_out,
            },
        )
        return json.dumps(
            {
                "ok": not timed_out,
                "exit_code": process.returncode,
                "stdout": out,
                "stderr": err,
                "truncated": out_truncated or err_truncated,
                "timed_out": timed_out,
                "duration_ms": duration_ms,
            }
        )
    except PathEscapeError as exc:
        return json.dumps({"ok": False, "error": str(exc)})
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"ok": False, "error": str(exc)})
