"""P4 slice 3: wake-on-backlog helpers."""

from __future__ import annotations

import asyncio

import pytest

from memnc.cli import _maybe_wake_idle
from memnc.config import Settings
from memnc.wol import WolError, WolWaker, magic_packet, parse_mac, send_wol


def test_parse_mac_accepts_colon_dash_and_case():
    assert parse_mac("AA:BB:CC:DD:EE:FF") == bytes.fromhex("aabbccddeeff")
    assert parse_mac("aa-bb-cc-dd-ee-ff") == bytes.fromhex("aabbccddeeff")
    assert parse_mac(" a0:b0:c0:d0:e0:f0 ") == bytes.fromhex("a0b0c0d0e0f0")


def test_parse_mac_rejects_malformed():
    for bad in ["", "aabbccddeeff", "aa:bb:cc:dd:ee", "zz:bb:cc:dd:ee:ff", "aa:bb:cc:dd:ee:ff:00"]:
        with pytest.raises(WolError):
            parse_mac(bad)


def test_magic_packet_structure():
    packet = magic_packet("AA:BB:CC:DD:EE:FF")
    assert len(packet) == 102
    assert packet[:6] == b"\xff" * 6
    target = bytes.fromhex("aabbccddeeff")
    assert packet[6:] == target * 16


def test_send_wol_broadcasts_one_datagram(monkeypatch):
    sent = []

    class FakeSocket:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def setsockopt(self, level, opt, value):
            assert (level, opt, value) == (__import__("socket").SOL_SOCKET, __import__("socket").SO_BROADCAST, 1)

        def sendto(self, data, addr):
            sent.append((data, addr))
            return len(data)

    monkeypatch.setattr("memnc.wol.socket.socket", FakeSocket)
    n = send_wol("AA:BB:CC:DD:EE:FF", port=7, bcast="10.0.0.255")
    assert n == 102
    assert len(sent) == 1
    data, (host, port) = sent[0]
    assert len(data) == 102
    assert (host, port) == ("10.0.0.255", 7)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeSender:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, mac: str, **kw) -> int:
        self.calls.append(mac)
        return 102


def test_waker_sends_when_enabled_and_throttles():
    clock, sender = FakeClock(), FakeSender()
    waker = WolWaker("AA:BB:CC:DD:EE:FF", 300, clock=clock, sender=sender)
    assert waker.maybe_wake() is True
    assert sender.calls == ["AA:BB:CC:DD:EE:FF"]
    clock.advance(100)
    assert waker.maybe_wake() is False
    clock.advance(201)  # 301 s since last wake
    assert waker.maybe_wake() is True
    assert len(sender.calls) == 2


def test_waker_disabled_without_mac():
    waker = WolWaker("", 300, clock=FakeClock, sender=FakeSender())
    assert waker.enabled is False
    assert waker.maybe_wake() is False


async def _store_fn(store):
    return store


class _FakeStore:
    def __init__(self, statuses: list[str]) -> None:
        self._statuses = statuses

    async def list(self):
        class _T:
            def __init__(self, status: str) -> None:
                self.status = status

        return [_T(s) for s in self._statuses]


def test_maybe_wake_idle_needs_planned_work_and_mac():
    settings = Settings(wol_mac="AA:BB:CC:DD:EE:FF", _env_file=None)

    def make_fn(store):
        async def _fn():
            return store
        return _fn

    # disabled waker -> no packet, no store touched
    quiet_no_mac = WolWaker("", 300, clock=FakeClock, sender=FakeSender())
    assert asyncio.run(_maybe_wake_idle(make_fn(_FakeStore([])), settings, quiet_no_mac)) is False
    # planned work + enabled waker -> packet sent
    store = _FakeStore(["planned", "done", "claimed"])
    waker = WolWaker("AA:BB:CC:DD:EE:FF", 300, clock=FakeClock, sender=FakeSender())
    assert asyncio.run(_maybe_wake_idle(make_fn(store), settings, waker)) is True
    # all non-planned -> no packet even with mac
    store = _FakeStore(["done", "failed"])
    quiet = WolWaker("AA:BB:CC:DD:EE:FF", 300, clock=FakeClock, sender=FakeSender())
    assert asyncio.run(_maybe_wake_idle(make_fn(store), settings, quiet)) is False


def test_wol_subparser_wired():
    from memnc.cli import build_parser

    args = build_parser().parse_args(["wol", "--mac", "AA:BB:CC:DD:EE:FF"])
    assert args.mac == "AA:BB:CC:DD:EE:FF"
    args = build_parser().parse_args(["wol"])
    assert args.mac == ""
