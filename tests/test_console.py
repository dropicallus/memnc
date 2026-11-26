"""Tests for the mesh console (S3)."""

import time

from memnc import console
from memnc.schema import GrantLevel, GrantRecord


class TestFormatGrants:
    def _grant(self, expires_at: float) -> GrantRecord:
        return GrantRecord(
            agent_id="node-b-agent",
            level=GrantLevel.ROOT,
            granted_by="root",
            expires_at=expires_at,
        )

    def test_never_expires(self):
        rows = console._format_grants([self._grant(0)])
        assert rows == [
            {
                "agent_id": "node-b-agent",
                "level": "root",
                "granted_by": "root",
                "expires": "never",
            }
        ]

    def test_expiry_seconds_remaining(self):
        rows = console._format_grants([self._grant(time.time() + 120)])
        assert rows[0]["expires"].startswith("in ")


class _FakeServer:
    server_address = ("127.0.0.1", 8765)


def _post(headers: dict[str, str], body: bytes = b'{"action": "cancel"}'):
    import io

    handler = console.ConsoleHandler.__new__(console.ConsoleHandler)
    handler.path = "/api/control"
    handler.headers = headers
    handler.server = _FakeServer()
    handler.rfile = io.BytesIO(body)
    handler.wfile = io.BytesIO()
    sent: list[int] = []
    handler._send = lambda code, data, ctype: sent.append(code)  # type: ignore[method-assign]
    handler.do_POST()
    return sent


def test_post_rejects_form_content_type():
    headers = {"Host": "127.0.0.1:8765", "Content-Type": "text/plain"}
    assert _post(headers) == [415]


def test_post_rejects_cross_origin():
    headers = {
        "Host": "127.0.0.1:8765",
        "Origin": "https://evil.example",
        "Content-Type": "application/json",
    }
    assert _post(headers) == [403]


def test_post_rejects_rebound_host():
    headers = {"Host": "rebound.example:8765", "Content-Type": "application/json"}
    assert _post(headers) == [403]


def test_console_page_has_no_inline_handlers():
    assert "onclick" not in console._PAGE
    assert 'data-action="cancel"' in console._PAGE
