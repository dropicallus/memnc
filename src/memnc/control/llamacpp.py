"""llama.cpp adapter: native ``llama-server`` processes, no llauncher needed.

llauncher's registry only knows what llauncher started. This adapter reads the
truth from the host: ``/proc/<pid>/cmdline`` for running servers, and the
process cgroup to find the systemd user unit that owns one. That gives
list/status/logs/stop/restart for every llama-server on the box, including the
ones somebody started by hand — which is exactly the case llauncher cannot see.

Serving spec is *not* guessed: an unmanaged process can be stopped, but
starting it again needs a unit (``systemctl --user start``). memnc will not
invent flags for a model.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time

import httpx

from .base import (
    FAILED,
    HEALTH_BAD,
    HEALTH_DOWN,
    HEALTH_OK,
    HEALTH_OPEN,
    RUNNING,
    STOPPED,
    ActionResult,
    BackendControl,
    BackendError,
    Service,
)

UNIT_RE = re.compile(r"([A-Za-z0-9@_.\-]+\.service)")


class LlamaCppControl:
    name = "llamacpp"

    def __init__(
        self,
        binary_name: str = "llama-server",
        units: str = "",
        proc_root: str = "/proc",
        health_timeout: float = 1.5,
    ) -> None:
        self.binary_name = binary_name
        self.units = [u.strip() for u in units.split(",") if u.strip()]
        self.proc_root = proc_root
        self.health_timeout = health_timeout

    # ── process discovery ──────────────────────────────────────────────────

    def _pids(self) -> list[int]:
        try:
            entries = os.listdir(self.proc_root)
        except OSError:
            return []
        return [int(e) for e in entries if e.isdigit()]

    def _cmdline(self, pid: int) -> list[str]:
        try:
            with open(f"{self.proc_root}/{pid}/cmdline", "rb") as fh:
                raw = fh.read()
        except OSError:
            return []
        return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]

    def _matches(self, argv: list[str]) -> bool:
        """True when the process *is* the server: argv[0] names the binary.

        Matching any of the first arguments made editors, greps and shells that
        merely mention the binary look like servers, and stop() could signal them.
        """
        return bool(argv) and os.path.basename(argv[0]) == self.binary_name

    def _unit(self, pid: int) -> str:
        try:
            with open(f"{self.proc_root}/{pid}/cgroup", encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            return ""
        return unit_from_cgroup(text)

    # ── reading ────────────────────────────────────────────────────────────

    def services(self) -> list[Service]:
        rows: list[Service] = []
        seen_units: set[str] = set()
        for pid in self._pids():
            argv = self._cmdline(pid)
            if not argv or not self._matches(argv):
                continue
            spec = parse_cmdline(argv)
            unit = self._unit(pid)
            if unit:
                seen_units.add(unit)
            endpoint = spec["endpoint"]
            rows.append(
                Service(
                    backend=self.name,
                    name=spec["name"] or f"pid-{pid}",
                    state=RUNNING,
                    health=self._health(endpoint),
                    endpoint=endpoint,
                    models=[spec["model"]] if spec["model"] else [],
                    service=unit or f"pid {pid}",
                    detail=_detail(spec),
                )
            )

        # Configured units that are not running right now still belong in the
        # list — a stopped backend you cannot see is a backend you cannot start.
        for unit in self.units:
            if unit in seen_units:
                continue
            state = self._unit_state(unit)
            rows.append(
                Service(
                    backend=self.name,
                    name=unit.removesuffix(".service"),
                    state=state,
                    health=HEALTH_DOWN if state != RUNNING else HEALTH_OPEN,
                    service=unit,
                    detail="configured unit",
                )
            )
        return rows

    def _health(self, endpoint: str) -> str:
        if not endpoint:
            return HEALTH_OPEN
        url = endpoint if "://" in endpoint else f"http://{endpoint}"
        try:
            resp = httpx.get(f"{url}/health", timeout=self.health_timeout)
        except Exception:  # noqa: BLE001 — dead port is a normal state
            return HEALTH_DOWN
        if resp.status_code != 200:
            return HEALTH_BAD
        try:
            payload = resp.json()
        except ValueError:
            return HEALTH_OPEN
        if isinstance(payload, dict) and payload.get("status") == "ok":
            return HEALTH_OK
        return HEALTH_OPEN

    # ── units ──────────────────────────────────────────────────────────────

    def _unit_state(self, unit: str) -> str:
        proc = self._systemctl("is-active", unit)
        active = (proc.stdout or "").strip()
        return RUNNING if active == "active" else (FAILED if active == "failed" else STOPPED)

    def _systemctl(self, *args: str) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                ["systemctl", "--user", *args],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BackendError(str(exc)) from exc

    def _service_for(self, name: str) -> Service | None:
        return next((svc for svc in self.services() if svc.name == name), None)

    # ── control ────────────────────────────────────────────────────────────

    def start(self, name: str) -> ActionResult:
        svc = self._service_for(name)
        if svc is None:
            return ActionResult(False, "start", self.name, name, "unknown llama-server")
        unit = svc.service if svc.service.endswith(".service") else ""
        if not unit:
            return ActionResult(
                False,
                "start",
                self.name,
                name,
                f"{name} is unmanaged (no systemd unit); memnc will not guess a launch spec",
            )
        proc = self._systemctl("start", unit)
        return _result("start", self.name, name, proc)

    def stop(self, name: str) -> ActionResult:
        svc = self._service_for(name)
        if svc is None:
            return ActionResult(False, "stop", self.name, name, "unknown llama-server")
        unit = svc.service if svc.service.endswith(".service") else ""
        if unit:
            return _result("stop", self.name, name, self._systemctl("stop", unit))

        pid = _pid_from_svc(svc.service)
        if pid is None:
            return ActionResult(False, "stop", self.name, name, "no pid recorded for this server")
        # The pid came from an earlier scan; the process may have exited and the
        # pid been reused. Signal only if it is still the server.
        if not self._matches(self._cmdline(pid)):
            return ActionResult(
                False, "stop", self.name, name, f"pid {pid} is no longer llama-server"
            )
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as exc:
            return ActionResult(False, "stop", self.name, name, str(exc))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not os.path.exists(f"{self.proc_root}/{pid}"):
                return ActionResult(True, "stop", self.name, name, f"SIGTERM to pid {pid}")
            time.sleep(0.2)
        return ActionResult(True, "stop", self.name, name, f"SIGTERM to pid {pid} (still exiting)")

    def restart(self, name: str) -> ActionResult:
        svc = self._service_for(name)
        if svc is None:
            return ActionResult(False, "restart", self.name, name, "unknown llama-server")
        unit = svc.service if svc.service.endswith(".service") else ""
        if not unit:
            return ActionResult(
                False,
                "restart",
                self.name,
                name,
                f"{name} is unmanaged (no systemd unit); stop it and start it by hand",
            )
        return _result("restart", self.name, name, self._systemctl("restart", unit))

    def logs(self, name: str, lines: int = 100) -> str:
        svc = self._service_for(name)
        if svc is None:
            raise BackendError(f"unknown llama-server {name!r}")
        unit = svc.service if svc.service.endswith(".service") else ""
        if unit:
            proc = self._systemctl("status", "--no-pager", "-n", str(lines), unit)
            return proc.stdout or proc.stderr
        pid = _pid_from_svc(svc.service)
        if pid is None:
            raise BackendError("no log source: process is gone and has no unit")
        return _tail_fd(pid, lines, self.proc_root)


def parse_cmdline(argv: list[str]) -> dict[str, str]:
    """Pull the parts of a llama-server command line memnc cares about."""
    takes_value = {
        "-m",
        "--model",
        "--port",
        "-p",
        "--host",
        "-c",
        "--ctx-size",
        "-md",
        "--model-draft",
    }
    found: dict[str, str] = {}
    i = 0
    while i < len(argv):
        arg = argv[i]
        value = None
        if arg.startswith("-") and "=" in arg:
            arg, value = arg.split("=", 1)
        if value is None and arg in takes_value and i + 1 < len(argv):
            value = argv[i + 1]
            i += 1
        if value is not None:
            found[arg] = value
        i += 1

    model = os.path.basename(found.get("-m") or found.get("--model") or "")
    draft = os.path.basename(found.get("-md") or found.get("--model-draft") or "")
    port = found.get("--port") or found.get("-p") or "8080"
    host = found.get("--host") or "127.0.0.1"
    if host in ("0.0.0.0", "::", "[::]"):
        host = "127.0.0.1"
    return {
        "model": model,
        "port": port,
        "host": host,
        "ctx": found.get("-c") or found.get("--ctx-size") or "",
        "draft": draft,
        "endpoint": f"{host}:{port}",
        "name": model.removesuffix(".gguf") if model else f"llama-server:{port}",
    }


def unit_from_cgroup(text: str) -> str:
    """Last ``*.service`` in a cgroup path, or '' for a hand-started process."""
    matches = UNIT_RE.findall(text)
    return matches[-1] if matches else ""


def _detail(spec: dict[str, str]) -> str:
    bits = []
    if spec.get("ctx"):
        bits.append(f"ctx {spec['ctx']}")
    if spec.get("draft"):
        bits.append(f"draft {spec['draft']}")
    return ", ".join(bits)


def _pid_from_svc(service: str) -> int | None:
    match = re.fullmatch(r"pid (\d+)", service.strip())
    return int(match.group(1)) if match else None


def _tail_fd(pid: int, lines: int, proc_root: str) -> str:
    """Best effort: serve logs when stdout was redirected to a regular file."""
    try:
        target = os.readlink(f"{proc_root}/{pid}/fd/1")
    except OSError as exc:
        raise BackendError(f"no log source for pid {pid}: {exc}") from exc
    if not os.path.isfile(target):
        raise BackendError(
            f"no log source for pid {pid}: stdout goes to {target} (not a file); "
            "run it under a systemd unit to get journal logs"
        )
    with open(target, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - 200_000))
        chunk = fh.read().decode("utf-8", "replace")
    return "\n".join(chunk.splitlines()[-lines:])


def _result(verb: str, backend: str, name: str, proc: subprocess.CompletedProcess) -> ActionResult:
    message = (proc.stderr or proc.stdout).strip()
    return ActionResult(
        ok=proc.returncode == 0,
        action=verb,
        backend=backend,
        name=name,
        message=message.splitlines()[-1] if message else "",
    )


assert isinstance(LlamaCppControl(), BackendControl)
