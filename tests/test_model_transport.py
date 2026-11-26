"""T6: model transport roundtrip against a real NATS (integration).

Enabled only when MEMNC_TEST_NATS is set (see tests/test_integration.py
for how to run a throwaway container).
"""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

import nats
import pytest

from memnc.model_transport import ModelTransport

NATS_URL = os.environ.get("MEMNC_TEST_NATS")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not NATS_URL, reason="MEMNC_TEST_NATS not set"),
]


@pytest.mark.asyncio
async def test_offer_fetch_roundtrip_sha256(tmp_path: Path) -> None:
    nc = await nats.connect(NATS_URL)
    try:
        # unique bucket suffix so repeated runs stay hermetic
        mt_offer = ModelTransport(nc, models_dir=tmp_path / "a", node="node-a")
        mt_fetch = ModelTransport(nc, models_dir=tmp_path / "b", node="node-b")

        payload = os.urandom(3 * (1 << 20) + 777)  # >2 morsels
        model_file = tmp_path / "test-model.gguf"
        model_file.write_bytes(payload)

        manifest = await mt_offer.offer_model(model_file)
        assert manifest["name"] == "test-model.gguf"
        assert manifest["sha256"] == hashlib.sha256(payload).hexdigest()
        assert manifest["node_sources"] == ["node-a"]

        fetched = await mt_fetch.fetch_model("test-model.gguf")
        assert fetched.read_bytes() == payload  # sha256-verified inside
        assert await mt_fetch.ensure_local("test-model.gguf") == fetched
    finally:
        await nc.close()


@pytest.mark.asyncio
async def test_fetch_unknown_manifest_raises(tmp_path: Path) -> None:
    nc = await nats.connect(NATS_URL)
    try:
        mt = ModelTransport(nc, models_dir=tmp_path, node="node-a")
        with pytest.raises(FileNotFoundError):
            await mt.fetch_model(f"does-not-exist-{uuid.uuid4().hex}.gguf")
        assert await mt.ensure_local(f"does-not-exist-{uuid.uuid4().hex}.gguf") is None
    finally:
        await nc.close()


def test_model_name_must_be_one_safe_component(tmp_path):
    import pytest

    from memnc.model_transport import _model_path

    assert _model_path(tmp_path, "qwen2.5-coder:7b") == tmp_path / "qwen2.5-coder:7b"
    for bad in ("../escape", "a/b", "/etc/passwd", ".hidden", "", "x\\y"):
        with pytest.raises(ValueError):
            _model_path(tmp_path, bad)
