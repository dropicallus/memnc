"""Unit tests for meshctl registry (cmd_registry) — without NATS.

cmd_registry instantiates `Bus(settings)` internally; therefore
`memnc.cli.Bus` is replaced via monkeypatch with a fake class whose
connect/close are no-ops and whose list_capabilities returns a
predefined list of Capability objects. The printed output is
verified via capsys.
"""

import pytest

from memnc import cli
from memnc.schema import Capability


class FakeBus:
    """No-op Bus that returns a predefined Capability list."""

    def __init__(self, settings=None):
        self.settings = settings
        self.closed = False

    async def connect(self):
        return None

    async def close(self):
        self.closed = True

    def set_registry(self, caps):
        self._caps = caps

    async def list_capabilities(self):
        return list(self._caps)


def _make_settings():
    # cmd_registry only needs Settings as an argument; a simple fake
    # object is fine because FakeBus never touches it.
    class _S:
        pass

    return _S()


def _cap(agent_id, skill, **kw):
    defaults = dict(node="n1", model="m1", contact="subject:w", load=0.5, capacity=2)
    defaults.update(kw)
    return Capability(agent_id=agent_id, skill=skill, **defaults)


@pytest.mark.asyncio
async def test_registry_empty(monkeypatch, capsys):
    fake = FakeBus()
    fake.set_registry([])
    monkeypatch.setattr(cli, "Bus", lambda settings: fake)

    rc = await cli.cmd_registry(__import__("argparse").Namespace(), _make_settings())
    out = capsys.readouterr().out
    assert rc == 0
    assert "no capabilities advertised" in out


@pytest.mark.asyncio
async def test_registry_lists_sorted_records(monkeypatch, capsys):
    fake = FakeBus()
    fake.set_registry(
        [
            _cap("beta", "code"),
            _cap("alpha", "web", node="n2", model="gemma4", contact="mcp://x"),
        ]
    )
    monkeypatch.setattr(cli, "Bus", lambda settings: fake)

    rc = await cli.cmd_registry(__import__("argparse").Namespace(), _make_settings())
    out = capsys.readouterr().out
    assert rc == 0
    # sorted by (agent_id, skill): alpha before beta
    assert out.index("alpha") < out.index("beta")
    assert "beta" in out and "code" in out
    assert "gemma4" in out
    assert "mcp://x" in out
    assert "2 capabilities advertised" in out


@pytest.mark.asyncio
async def test_registry_closes_bus(monkeypatch):
    fake = FakeBus()
    fake.set_registry([])
    monkeypatch.setattr(cli, "Bus", lambda settings: fake)

    await cli.cmd_registry(__import__("argparse").Namespace(), _make_settings())
    assert fake.closed is True
