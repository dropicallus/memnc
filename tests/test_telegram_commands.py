"""Pure-logic tests for the improved Telegram command formatting."""

import json

import pytest

from memnc.telegram import format_roster, format_task, summarize_tasks


def snap(task_id: str, state: str, claimed_by: str = "", task: str = "do the thing") -> dict:
    return {
        "task_id": task_id,
        "state": state,
        "claimed_by": claimed_by,
        "task": task,
    }


def test_summary_header_counts_by_state():
    snaps = [snap(f"id{i}", "FAILED") for i in range(3)] + [snap("x", "DONE")]
    out = summarize_tasks(snaps)
    assert out.startswith("4 tasks: FAILED=3 DONE=1")


def test_summary_shows_active_first_then_recent():
    snaps = [snap(f"old{i}", "DONE") for i in range(20)] + [
        snap("new", "CRITIQUE", "node-a-critic:critic")
    ]
    out = summarize_tasks(snaps, limit=5)
    lines = out.splitlines()
    # active task appears before any terminal one
    idx_active = next(i for i, ln in enumerate(lines) if "#new" in ln)
    idx_done = min(i for i, ln in enumerate(lines) if "#old" in ln)
    assert idx_active < idx_done
    assert len([ln for ln in lines if ln.startswith("#")]) == 6  # 1 active + 5 recent


def test_summary_filter_state():
    snaps = [snap("a", "FAILED"), snap("b", "DONE")]
    out = summarize_tasks(snaps, "failed")
    assert "#a FAILED" in out and "b" not in out.split("showing")[1]


def test_summary_filter_no_match():
    snaps = [snap("a", "DONE")]
    assert "no tasks with state: failed" in summarize_tasks(snaps, "failed")


def test_summary_includes_task_title():
    out = summarize_tasks([snap("a", "DONE", task="print T6-GATE-OK")])
    assert "print T6-GATE-OK" in out


def test_summary_empty():
    assert summarize_tasks([]) == "no tasks in ledger"


# -- M11b: Telegram routes control commands through the mesh API -------------


class _FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, chat: str, text: str) -> None:
        self.sent.append((chat, text))


class _FakeNc:
    def __init__(self, reply: dict) -> None:
        self.reply = reply
        self.subjects: list[str] = []
        self.raw: list[bytes] = []

    async def request(self, subject: str, payload: bytes, timeout: float = 5):
        self.subjects.append(subject)
        self.raw.append(payload)
        return type("M", (), {"data": json.dumps(self.reply).encode()})()


def _observer(reply: dict):

    from memnc.config import Settings
    from memnc.telegram import TelegramObserver

    nc = _FakeNc(reply)
    obs = TelegramObserver(Settings(node_name="t", telegram_allow="42"), _FakeBot())  # type: ignore[arg-type]
    obs.bus.nc = nc  # type: ignore[assignment]
    return obs, nc


def test_control_submit_goes_through_api_write_path():
    import asyncio
    import json as _json

    obs, nc = _observer({"ok": True, "task_id": "t9"})
    # api_request rides on obs.bus.nc (the fake) — no NATS server needed
    asyncio.run(obs._control("42", "submit", "audit the mesh"))
    # Telegram is an API client: the request hits the API subject, not mesh.control
    assert nc.subjects == ["mesh.api.request.task"]
    assert _json.loads(nc.raw[0]) == {
        "verb": "task.submit",
        "args": {"agent_id": "telegram-42", "task": "audit the mesh"},
    }
    assert obs.bot.sent == [("42", "submitted: #t9")]


def test_control_denied_is_surfaced():
    import asyncio
    import json as _json

    obs, nc = _observer({"error": "not granted"})
    asyncio.run(obs._control("42", "cancel", "#t1"))
    assert nc.subjects == ["mesh.api.request.task"]
    assert _json.loads(nc.raw[0])["args"] == {"agent_id": "telegram-42", "task_id": "t1"}
    assert obs.bot.sent == [("42", "denied: api task.cancel: not granted")]


def test_format_roster_lines():
    nodes = [
        {
            "node_id": "node-a-coder",
            "roles": ["coder"],
            "models": ["llama3.2:3b"],
            "vram_gb": 16.0,
            "gpu": "test-gpu-a",
        }
    ]
    out = format_roster(nodes)
    assert "1 nodes:" in out
    assert (
        "node-a-coder roles=coder models=llama3.2:3b vram=16GB gpu=test-gpu-a" in out
    )  # leakscan:ok


def test_format_roster_empty():
    assert format_roster([]) == "no nodes in roster"


def test_format_task_shows_title():
    out = format_task(snap("abc123", "DONE", claimed_by="node-a-critic:critic"))
    assert "#abc123 DONE" in out
    assert "do the thing" in out


# ---- /nodes list via the mesh API (M11a) ------------------------------------


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_nodes_command_uses_mesh_api(monkeypatch):
    import types

    from memnc.telegram import TelegramObserver

    sent = []
    nodes = [
        {
            "node_id": "node-a-coder",
            "roles": ["coder"],
            "models": ["llama3.2:3b"],
            "vram_gb": 16.0,
            "gpu": "test-gpu-a",
        }
    ]

    async def fake_api_request(bus, verb, **kwargs):
        assert verb == "node.list"
        return {"ok": True, "data": nodes}

    monkeypatch.setattr("memnc.api.api_request", fake_api_request)

    class FakeBot:
        async def send_message(self, chat, text):
            sent.append(text)

    obs = types.SimpleNamespace(bot=FakeBot(), bus=None, allowed=set())
    await TelegramObserver.handle_command(obs, "chat1", "/nodes")  # type: ignore[attr-defined]
    out = sent[0]
    assert "1 nodes:" in out
    assert "node-a-coder roles=coder" in out  # leakscan:ok


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_node_without_arg_lists_too(monkeypatch):
    import types

    from memnc.telegram import TelegramObserver

    sent = []

    async def fake_api_request(bus, verb, **kwargs):
        assert verb == "node.list"
        return {"ok": True, "data": []}

    monkeypatch.setattr("memnc.api.api_request", fake_api_request)

    class FakeBot:
        async def send_message(self, chat, text):
            sent.append(text)

    obs = types.SimpleNamespace(bot=FakeBot(), bus=None)
    await TelegramObserver.handle_command(obs, "chat1", "/node")  # type: ignore
    assert sent == ["no nodes in roster"]


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_node_with_name_still_details(monkeypatch):
    import types

    from memnc.telegram import TelegramObserver

    sent = []

    async def fake_api_request(bus, verb, **kwargs):
        assert verb == "node.detail" and kwargs == {"node_id": "node-a-coder"}
        return {"ok": True, "data": {"node": {"node_id": "node-a-coder"}, "tasks": []}}

    monkeypatch.setattr("memnc.api.api_request", fake_api_request)

    class FakeBot:
        async def send_message(self, chat, text):
            sent.append(text)

    obs = types.SimpleNamespace(bot=FakeBot(), bus=None)
    await TelegramObserver.handle_command(obs, "chat1", "/node node-a-coder")  # type: ignore
    assert "node node-a-coder" in sent[0]
