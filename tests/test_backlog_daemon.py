"""Daemonization of the backlog driver: launcher script + systemd user unit.

The driver loop itself lives in cli.cmd_backlog_run; this slice makes it run
as a service without a fragile nohup. Tests pin the invariants the skill
rules require: no `source .env`, no empty-token export, loopback URL default,
PYTHONPATH stripped, Restart policy present.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "backlog-driver.sh"
UNIT = REPO / "scripts" / "systemd" / "memnc-backlog-driver.service"


def test_launcher_exists_and_executable() -> None:
    assert SCRIPT.is_file(), f"missing {SCRIPT}"
    assert os.access(SCRIPT, os.X_OK), "launcher must be executable"


def test_launcher_never_sources_env() -> None:
    text = SCRIPT.read_text()
    assert "\nsource " not in text and "\n. " not in text.replace("\n. \"", "\nX")
    assert "awk -F= -v k=NATS_TOKEN" in text, "token must be extracted, not sourced"


def test_launcher_never_exports_empty_token() -> None:
    text = SCRIPT.read_text()
    assert 'if [ -n "$nats_token" ]; then' in text
    # The guarded export must be inside the if-block, not a `[ -n ] && export`
    # chain (returns 1 under set -e when empty).
    assert "[ -n" not in text.split("exec ")[0].replace(
        'if [ -n "$nats_token" ]; then', ""
    )


def test_launcher_defaults_to_loopback_url() -> None:
    text = SCRIPT.read_text()
    assert "nats://127.0.0.1:4222" in text, "host-side driver needs loopback"
    # The .env NATS_URL is the compose-internal hostname and must not be used.
    assert "awk -F= -v k=NATS_URL" not in text


def test_launcher_strips_pythonpath() -> None:
    assert "-u PYTHONPATH" in SCRIPT.read_text()


def test_launcher_runs_backlog_run() -> None:
    assert "backlog-run" in SCRIPT.read_text()


def test_unit_restarts_on_failure() -> None:
    text = UNIT.read_text()
    assert "Restart=on-failure" in text
    assert "RestartSec" in text
    assert "ExecStart=%h/memnc/scripts/backlog-driver.sh" in text
    assert "WantedBy=default.target" in text


def test_unit_needs_no_root() -> None:
    text = UNIT.read_text()
    lowered = text.lower()
    assert "sudo" not in lowered and "user=" not in lowered
