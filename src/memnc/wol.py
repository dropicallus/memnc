"""Wake-on-LAN (P4 slice 3: wake-on-backlog).

When the backlog holds planned work and the configured cold-reserve host
is asleep, the driver sends a magic packet to wake it. Pure helpers here:
MAC parsing, packet construction, throttled wake decisions. The actual
UDP send is one small function; tests inject a fake socket.
"""

from __future__ import annotations

import socket
import time
from collections.abc import Callable


class WolError(ValueError):
    """Raised for a malformed MAC address."""


def parse_mac(mac: str) -> bytes:
    """Parse ``aa:bb:cc:dd:ee:ff`` (colons or hyphens, any case) to 6 bytes."""
    raw = mac.strip().replace("-", ":")
    parts = raw.split(":")
    if len(parts) != 6:
        raise WolError(f"invalid MAC address: {mac!r}")
    try:
        return bytes(int(part, 16) for part in parts)
    except ValueError as exc:
        raise WolError(f"invalid MAC address: {mac!r}") from exc


def magic_packet(mac: str) -> bytes:
    """Standard WOL frame: 6 x 0xFF followed by the MAC repeated 16 times."""
    return b"\xff" * 6 + parse_mac(mac) * 16


def send_wol(
    mac: str, *, port: int = 9, bcast: str = "255.255.255.255"
) -> int:
    """Send one magic packet as a broadcast UDP datagram. Returns bytes sent."""
    packet = magic_packet(mac)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        return sock.sendto(packet, (bcast, port))


class WolWaker:
    """Throttled wake decision for the driver loop."""

    def __init__(
        self,
        mac: str,
        min_interval_s: float = 300.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sender: Callable[..., int] = send_wol,
    ) -> None:
        self.mac = mac.strip()
        self.min_interval_s = min_interval_s
        self._clock = clock
        self._sender = sender
        self._last_wake: float | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.mac)

    def maybe_wake(self) -> bool:
        """Send a packet if enough time has passed. True when a packet was sent."""
        if not self.enabled:
            return False
        now = self._clock()
        if self._last_wake is not None and now - self._last_wake < self.min_interval_s:
            return False
        self._sender(self.mac)
        self._last_wake = now
        return True
