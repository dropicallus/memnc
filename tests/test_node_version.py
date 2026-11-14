"""P4 slice 2: node code fingerprints and stale-node refusal."""

from __future__ import annotations

import asyncio
from pathlib import Path

from test_m14 import SilentBus

from memnc.config import Settings
from memnc.schema import Envelope, Kind, NodeInfo, Role
from memnc.version import code_version, compute_version, is_stale
from memnc.worker import Worker


def _pkg_tree(tmp_path: Path, marker: str) -> Path:
    pkg = tmp_path / "pkg"
    pkg.mkdir(exist_ok=True)
    (pkg / "a.py").write_text(f"X = {marker}\n")
    (pkg / "sub").mkdir(exist_ok=True)
    (pkg / "sub" / "b.py").write_text("Y = 1\n")
    return pkg


def test_compute_version_deterministic_and_sensitive(tmp_path):
    a = compute_version(_pkg_tree(tmp_path, "1"))
    b = compute_version(_pkg_tree(tmp_path, "1"))
    c = compute_version(_pkg_tree(tmp_path, "2"))
    assert a == b
    assert a != c  # any source change moves the fingerprint
    assert "+" in a and len(a.rsplit("+", 1)[1]) == 12


def test_is_stale_fail_open_on_unknown():
    mine = code_version()
    assert not is_stale(mine, mine)
    assert not is_stale("dev", mine)  # unknown build: never refused
    assert not is_stale("", mine)
    assert is_stale(mine.rsplit("+", 1)[0] + "+deadbeefdead", mine)
    # Both sides without hash component -> unknown, fail-open.
    assert not is_stale("dev", "dev")


def test_node_info_merge_carries_version():
    fresh = NodeInfo(node_id="n", version="0.1.0+abc123def456")
    dev = NodeInfo(node_id="n", version="dev")
    assert fresh.merge(dev).version == "0.1.0+abc123def456"
    assert dev.merge(fresh).version == "0.1.0+abc123def456"
    # Default merge keeps the default.
    assert NodeInfo(node_id="n").merge(NodeInfo(node_id="n")).version == "dev"


class _NakMsg:
    def __init__(self, envelope: Envelope):
        self.data = envelope.model_dump_json().encode()
        self.naked = False
        self.acked = False

    async def nak(self, delay: float | None = None):
        self.naked = True

    async def ack(self):
        self.acked = True


def _cmd(mesh_version: str | None) -> Envelope:
    payload: dict = {"task": "x", "role": "coder"}
    if mesh_version is not None:
        payload["mesh_version"] = mesh_version
    return Envelope(
        task_id="t1",
        trace_id="t1",
        kind=Kind.COMMAND,
        role=Role.CODER,
        payload=payload,
    )


def test_worker_refuses_stale_command_when_enforced():
    bus = SilentBus(Settings())
    w = Worker(Settings(role="coder", mesh_claim=True, enforce_node_version=True), bus=bus)
    mine = code_version()
    stale = mine.rsplit("+", 1)[0] + "+000000000000"
    msg = _NakMsg(_cmd(stale))
    asyncio.run(w._handle(msg))
    assert msg.naked and not msg.acked


def test_worker_accepts_matching_command_when_enforced():
    bus = SilentBus(Settings())
    w = Worker(Settings(role="coder", mesh_claim=True, enforce_node_version=True), bus=bus)
    # Matching version: the gate passes and handling continues to the
    # snapshot lookup (unknown task -> dead letter + ack on this fake bus).
    msg = _NakMsg(_cmd(code_version()))
    asyncio.run(w._handle(msg))
    assert not msg.naked and msg.acked


def test_worker_accepts_stale_command_when_not_enforced():
    bus = SilentBus(Settings())
    w = Worker(Settings(role="coder", mesh_claim=True), bus=bus)
    stale = code_version().rsplit("+", 1)[0] + "+000000000000"
    msg = _NakMsg(_cmd(stale))
    asyncio.run(w._handle(msg))
    # Enforcement off (default): old fleets keep working, no refusal.
    assert not msg.naked and msg.acked


def test_worker_accepts_missing_mesh_version():
    bus = SilentBus(Settings())
    w = Worker(Settings(role="coder", mesh_claim=True, enforce_node_version=True), bus=bus)
    msg = _NakMsg(_cmd(None))
    asyncio.run(w._handle(msg))
    # An old orchestrator that stamps nothing must not be refused.
    assert not msg.naked and msg.acked


def test_dispatch_stamps_mesh_version():
    """The orchestrator fingerprints every dispatch command it publishes."""
    import memnc.orchestrator as orch_mod
    from memnc.schema import TaskSnapshot

    orch = orch_mod.Orchestrator.__new__(orch_mod.Orchestrator)
    orch.settings = Settings(node_name="node-a", workspace_root="/tmp/ws")
    orch.bus = SilentBus(orch.settings)
    sent: list = []

    async def fake_publish(subject, envelope):
        sent.append((subject, envelope))

    orch.bus.publish_envelope = fake_publish  # type: ignore[method-assign]

    async def fake_put_snapshot(snapshot):
        pass

    orch.bus.put_snapshot = fake_put_snapshot  # type: ignore[method-assign]

    async def fake_emit(*a, **k):
        pass

    orch.bus.emit_event = fake_emit  # type: ignore[method-assign]

    snap = TaskSnapshot(task_id="t9", trace_id="t9", task="do it")
    asyncio.run(orch._dispatch(snap, Role.CODER))
    subject, env = sent[0]
    assert "coder" in subject
    assert env.payload["mesh_version"] == code_version()
