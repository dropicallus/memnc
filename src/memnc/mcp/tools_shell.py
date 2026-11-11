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

        # Minimal env: model-driven shell commands must not inherit secrets
        # like NATS_TOKEN or INFERENCE_API_KEY from os.environ.
        shell_env = {
            k: os.environ[k]
            for k in ("PATH", "HOME", "LANG", "TERM", "TMPDIR")
            if k in os.environ
        }
        process = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            cmd,
            cwd=str(workdir),
            env=shell_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )

        def _kill() -> None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        # Bounded incremental reads: never buffer unbounded process output.
        async def _read_stream(stream, limit) -> tuple[bytes, bool]:
            chunks: list[bytes] = []
            truncated = False
            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    break
                if len(chunks) * 65536 < limit:
                    room = limit - sum(len(c) for c in chunks)
                    chunks.append(chunk[:room])
                    if len(chunk) > room:
                        truncated = True
                else:
                    truncated = True
                # keep draining to EOF, discarding beyond the cap
            return b"".join(chunks), truncated

        stdout_task = asyncio.ensure_future(_read_stream(process.stdout, max_output_bytes))
        stderr_task = asyncio.ensure_future(_read_stream(process.stderr, max_output_bytes))
        timed_out = False
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            timed_out = True
        finally:
            # kill+reap on timeout AND on task cancellation (zombie prevention)
            if process.returncode is None:
                _kill()
                try:
                    await process.wait()
                except Exception:  # noqa: BLE001
                    pass
        stdout, out_truncated = await stdout_task
        stderr, err_truncated = await stderr_task

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
                "stdout": stdout.decode("utf-8", errors="replace"),
                "stderr": stderr.decode("utf-8", errors="replace"),
                "truncated": out_truncated or err_truncated,
                "timed_out": timed_out,
                "duration_ms": duration_ms,
            }
        )
    except PathEscapeError as exc:
        return json.dumps({"ok": False, "error": str(exc)})
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"ok": False, "error": str(exc)})
