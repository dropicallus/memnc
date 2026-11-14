"""Tests for the M11d model-mediated API agent (src/memnc/api_agent.py).

The LLM call is stubbed at _chat — no network in tests.
"""

from __future__ import annotations

import json

import pytest

from memnc.api_agent import ApiAgent
from tests.test_api import FakeBus


def _settings(**kw):
    from memnc.config import Settings

    return Settings(node_name="agent-node", **kw)


class _RosterBus(FakeBus):
    """FakeBus + the governance reads the agent may touch."""


class _Msg:
    def __init__(self, data: bytes) -> None:
        self.data = data


def _fake_nc(payload: dict):
    """Stand-in for bus.nc: api_request() sends one request, gets one reply."""

    class _N:
        async def request(self, subject, payload_bytes, timeout=10):
            _fake_nc.subject = subject
            return _Msg(json.dumps(payload).encode())

    return _N()


def _agent(monkeypatch, reply: str, **kw) -> tuple[ApiAgent, _RosterBus]:
    bus = _RosterBus()
    agent = ApiAgent(_settings(**kw), bus)  # type: ignore[arg-type]

    async def fake_chat(messages):
        fake_chat.messages = messages
        return reply

    fake_chat.messages = []
    monkeypatch.setattr(agent, "_chat", fake_chat)
    return agent, bus


@pytest.mark.asyncio
async def test_ask_routes_nl_to_api_verb(monkeypatch):
    agent, _bus = _agent(
        monkeypatch,
        json.dumps({"verb": "node.detail", "args": {"node_id": "node-a"}}),
    )
    _bus.nc = _fake_nc(
        {"ok": True, "data": {"node": {"node_id": "node-a", "roles": ["coder"]}, "tasks": []}}
    )
    out = await agent.ask("Was macht node-a gerade?")
    assert "node node-a roles=coder" in out
    assert "no tasks" not in out or "keine Tasks" in out


@pytest.mark.asyncio
async def test_ask_submit_returns_task_id(monkeypatch):
    class SubmitBus(FakeBus):
        async def read_ledger(self):
            return {}

    bus = SubmitBus()

    bus.nc = _fake_nc({"ok": True, "data": {}})

    agent = ApiAgent(_settings(), bus)  # type: ignore[arg-type]

    async def fake_chat(messages):
        return json.dumps({"verb": "task.submit", "args": {"task": "audit the mesh"}})

    monkeypatch.setattr(agent, "_chat", fake_chat)
    out = await agent.ask("Bitte audit the mesh anlegen")
    assert "submitted" in out
    assert "mesh.api.request.task" == _fake_nc.subject


@pytest.mark.asyncio
async def test_ask_refuses_disallowed_verb(monkeypatch):
    agent, _bus = _agent(
        monkeypatch,
        json.dumps({"verb": "node.stop_worker", "args": {"worker": "x"}}),
        agent_allowed_verbs="task.list,node.list",
    )
    out = await agent.ask("Stop the worker")
    assert "verweigert" in out and "node.stop_worker" in out


@pytest.mark.asyncio
async def test_ask_handles_non_api_question(monkeypatch):
    agent, _bus = _agent(monkeypatch, json.dumps({"verb": "", "args": {}}))
    out = await agent.ask("Erzähl mir einen Witz")
    assert "keine Mesh-Afrage" in out


@pytest.mark.asyncio
async def test_ask_surfaces_api_error(monkeypatch):
    agent, _bus = _agent(
        monkeypatch,
        json.dumps({"verb": "task.detail", "args": {"task_id": "missing"}}),
    )
    out = await agent.ask("Wo ist task missing?")
    assert "Fehler bei task.detail" in out


def test_parse_plan_tolerates_fences():
    assert ApiAgent._parse_plan('```json\n{"verb": "task.list", "args": {}}\n```') == (
        "task.list",
        {},
    )
    assert ApiAgent._parse_plan("no json here") == ("", {})
    assert ApiAgent._parse_plan('{"verb": "task.list", "args": null}') == ("task.list", {})
