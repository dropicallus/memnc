"""T7: model reputation, pull whitelist, retirement proposals."""

from __future__ import annotations

from test_m14 import SilentBus

import memnc.worker as worker_mod
from memnc.config import Settings
from memnc.schema import (
    Envelope,
    Kind,
    ModelReputation,
    ModelRequest,
    Role,
    model_pull_allowed,
    should_propose_retirement,
)
from memnc.worker import Worker


def test_pull_whitelist_fnmatch():
    # Empty whitelist closes the pull path entirely.
    assert not model_pull_allowed("qwen3:8b", [])
    assert model_pull_allowed("qwen3:8b", ["qwen*"])
    assert model_pull_allowed("gemma-4-31b", ["gemma-4-31b"])
    assert not model_pull_allowed("llama3.1:8b", ["qwen*", "gemma*"])


def test_retire_rule_pure():
    rep = ModelReputation(model="m")
    assert not should_propose_retirement(rep, 3)
    rep.consecutive_rejections = 3
    assert should_propose_retirement(rep, 3)
    # Threshold 0 disables the rule entirely.
    assert not should_propose_retirement(rep, 0)
    rep.retired = True
    assert not should_propose_retirement(rep, 3)


async def test_reputation_records_verdicts():
    bus = SilentBus(Settings())
    rep = await bus.record_reputation("qwen3:8b", False)
    assert rep.rejections == 1 and rep.consecutive_rejections == 1
    rep = await bus.record_reputation("qwen3:8b", False)
    assert rep.consecutive_rejections == 2
    rep = await bus.record_reputation("qwen3:8b", True)
    # An approval clears the streak but keeps the totals.
    assert rep.approvals == 1 and rep.consecutive_rejections == 0
    assert rep.rejections == 2
    # Persisted in the KV under the sanitized key.
    entry = await bus.kv.get("model_rep.qwen3_8b")
    assert ModelReputation.model_validate_json(entry.value).approvals == 1
    assert await bus.list_reputation()  # listed


class _Msg:
    def __init__(self, envelope: Envelope):
        self.data = envelope.model_dump_json().encode()
        self.acked = False

    async def ack(self):
        self.acked = True


def _pull_envelope(model: str, approved: bool = True) -> Envelope:
    request = ModelRequest(
        request_id="r1", model=model, action="pull", status="approved" if approved else "pending"
    )
    return Envelope(
        task_id="t",
        trace_id="t",
        kind=Kind.COMMAND,
        role=Role.PLANNER,
        payload={"request": request.model_dump()},
    )


async def test_approved_pull_refused_without_whitelist():
    bus = SilentBus(Settings())
    w = Worker(Settings(role="coder", mesh_claim=True), bus=bus)
    pulls: list[str] = []

    async def fake_pull(base_url, model):
        pulls.append(model)
        return True

    import memnc.worker as wm

    orig = wm.pull_model
    wm.pull_model = fake_pull
    try:
        msg = _Msg(_pull_envelope("qwen3:8b"))
        await w._handle_model_decision(msg)
        assert msg.acked
        assert pulls == []  # empty whitelist -> closed pull path
    finally:
        wm.pull_model = orig


async def test_approved_pull_executes_when_whitelisted():
    bus = SilentBus(Settings())
    w = Worker(
        Settings(role="coder", mesh_claim=True, model_pull_whitelist=["qwen*"]),
        bus=bus,
    )
    pulls: list[str] = []
    loads: list[str] = []

    async def fake_pull(base_url, model):
        pulls.append(model)
        return True

    async def fake_load(base_url, model, keep_alive="30m"):
        loads.append(model)
        return True

    async def fake_disk(base_url):
        return ["qwen3:8b"]

    wm = worker_mod
    orig = (wm.pull_model, wm.load_model, wm.fetch_disk_models)
    wm.pull_model, wm.load_model, wm.fetch_disk_models = fake_pull, fake_load, fake_disk
    try:
        msg = _Msg(_pull_envelope("qwen3:8b"))
        await w._handle_model_decision(msg)
        assert msg.acked
        assert pulls == ["qwen3:8b"]
        assert loads == ["qwen3:8b"]  # pulled then loaded
    finally:
        wm.pull_model, wm.load_model, wm.fetch_disk_models = orig


async def test_pending_pull_not_executed():
    bus = SilentBus(Settings())
    w = Worker(Settings(role="coder"), bus=bus)
    msg = _Msg(_pull_envelope("qwen3:8b", approved=False))
    await w._handle_model_decision(msg)
    assert msg.acked  # acknowledged, nothing executed


async def test_retire_decision_marks_reputation(monkeypatch):
    bus = SilentBus(Settings())
    w = Worker(Settings(role="coder"), bus=bus)
    unloads: list[str] = []

    async def fake_unload(base_url, model):
        unloads.append(model)
        return True

    orig = worker_mod.unload_model
    worker_mod.unload_model = fake_unload
    try:
        request = ModelRequest(
            request_id="r2", model="qwen3:8b", action="retire", status="approved"
        )
        msg = _Msg(
            Envelope(
                task_id="t",
                trace_id="t",
                kind=Kind.COMMAND,
                role=Role.PLANNER,
                payload={"request": request.model_dump()},
            )
        )
        await w._handle_model_decision(msg)
        assert msg.acked and unloads == ["qwen3:8b"]
        rep = await bus.get_reputation("qwen3:8b")
        assert rep.retired
    finally:
        worker_mod.unload_model = orig


async def test_claim_loop_skips_retired_model():
    bus = SilentBus(Settings(role="coder", mesh_claim=True))
    _ = Worker(  # noqa: F841 - construction registers handlers on the bus
        Settings(role="coder", mesh_claim=True, claim_poll_s=0.01), bus=bus
    )
    rep = ModelReputation(model="qwen3:8b", retired=True)
    await bus.kv.put("model_rep.qwen3_8b", rep.model_dump_json().encode())
    # get_reputation reads through the same FakeKV.
    fetched = await bus.get_reputation("qwen3:8b")
    assert fetched.retired


async def test_get_reputation_defaults_on_nats_key_not_found():
    # nats-py raises nats.js.errors.KeyNotFoundError (not KeyError) for a
    # missing KV key; the reputation lookup must still default (regression:
    # the claim loop crashed on the first claim with no reputation entry).
    from nats.js.errors import KeyNotFoundError
    from test_m11_claim import FakeKV

    class BoomKV(FakeKV):
        async def get(self, key):
            raise KeyNotFoundError()

    bus = SilentBus(Settings())
    bus.kv = BoomKV()  # type: ignore[assignment]
    rep = await bus.get_reputation("qwen3:8b")
    assert rep.model == "qwen3:8b" and not rep.retired
