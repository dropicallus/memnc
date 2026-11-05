"""Unit-Tests fuer meshctl registry (cmd_registry) — ohne NATS.

cmd_registry instanziiert intern selbst `Bus(settings)`; daher wird
`memnc.cli.Bus` hier per monkeypatch durch eine Fake-Klasse ersetzt,
deren connect/close no-ops sind und list_capabilities eine vorgegebene
Liste von Capability-Ojekten zurueckgibt. Die gedruckte Ausgabe wird
ueber capsys geprueft.
"""

import pytest

from memnc import cli
from memnc.schema import Capability


class FakeBus:
    """No-op Bus, der eine vorbelegte Capability-Liste liefert."""

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
    # cmd_registry braucht nur die Settings als Argument; wir koennen ein
    # einfaches Fake-Objekt nutzen, da FakeBus es nicht anfasst.
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
    # sortiert nach (agent_id, skill): alpha vor beta
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
