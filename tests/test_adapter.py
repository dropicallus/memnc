"""Unit tests for the M7 capability registry model and the agent adapter.

These run without NATS: they only exercise the pure schema/validation parts of
:mod:`memnc.schema` (``Capability.key`` / ``from_key`` / JSON round-trip) and
the adapter's plain logic (dot-check, handler plumbing).  Anything needing a
live bus belongs in tests/test_integration.py / test_worker.py.
"""

import pytest
from pydantic import ValidationError

from memnc.adapter import AgentAdapter, _check_clean
from memnc.schema import Capability


# -- Capability registry key -------------------------------------------------
def test_capability_key_layout():
    cap = Capability(agent_id="hermes-t", skill="code", node="n1", contact="cli:hermes")
    assert cap.key() == "registry.hermes-t.code"


def test_capability_from_key_roundtrip():
    cap = Capability(agent_id="hermes-t", skill="code")
    assert Capability.from_key(cap.key()) == ("hermes-t", "code")


def test_capability_from_key_rejects_unknown_prefix():
    assert Capability.from_key("other.hermes-t.code") is None


def test_capability_from_key_rejects_wrong_arity():
    assert Capability.from_key("registry.too.many.parts") is None


def test_capability_json_roundtrip():
    cap = Capability(
        agent_id="agent-1",
        skill="review",
        node="n2",
        model="gemma4",
        contact="mcp://n2:agent-1",
        load=0.5,
        capacity=2,
    )
    restored = Capability.model_validate_json(cap.model_dump_json())
    assert restored == cap
    assert restored.load == 0.5
    assert restored.contact == "mcp://n2:agent-1"


def test_capability_defaults():
    cap = Capability(agent_id="a1", skill="code")
    assert cap.capacity == 1
    assert cap.load == 0.0
    assert cap.node == ""
    assert cap.ts > 0


# -- adapter: dot safety -----------------------------------------------------
def test_adapter_rejects_dotted_ids_and_skills():
    with pytest.raises(ValueError):
        _check_clean(agent_id="host.local", skill="code")
    with pytest.raises(ValueError):
        _check_clean(agent_id="ok", skill="code.review")


def test_adapter_accepts_clean_ids():
    # Should not raise.
    _check_clean(agent_id="hermes-t", skill="code")


# -- adapter: handler plumbing (no NATS) -------------------------------------
@pytest.mark.asyncio
async def test_adapter_echo_handler_echoes():
    adapter = AgentAdapter(agent_id="t1", node="n1")  # default handler = _echo
    result = await adapter.handler({"task_id": "x", "hello": 1})
    assert result["ok"] is True
    assert result["echo"]["hello"] == 1


@pytest.mark.asyncio
async def test_adapter_custom_async_handler():
    async def handler(payload):
        return {"ok": True, "got": payload["task"]}

    adapter = AgentAdapter(agent_id="t2", node="n1", handler=handler)
    assert await adapter.handler({"task": "do x"}) == {"ok": True, "got": "do x"}


def test_adapter_defaults():
    adapter = AgentAdapter(agent_id="a1", node="n9")
    assert adapter.contact == "mcp://n9:a1"
    assert adapter.skills == ["code"]
