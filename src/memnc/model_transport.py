"""T6: NATS model transport — GGUF via JetStream Object Store.

offer_model(node, path): chunked upload (~1 MiB morsels, fine on LAN for
5-20 GB files) + manifest publish to KV `model-manifest`.
fetch_model(name): manifest lookup → download to ~/.memnc/models/ →
sha256-verify. The `ensure` intent uses this when a model is missing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

from nats.js import JetStreamContext
from nats.js.errors import KeyNotFoundError

log = logging.getLogger(__name__)

# Manifest names come from the bus: one path component, no separators, no
# leading dot, so they cannot leave the models directory.
_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*")


def _model_path(models_dir: Path, name: str) -> Path:
    if not _MODEL_NAME.fullmatch(name):
        raise ValueError(f"refusing model name {name!r}")
    return models_dir / name

OBJECT_STORE_BUCKET = "gguf"
KV_MANIFEST = "model-manifest"
MORSEL = 1 << 20  # 1 MiB
DEFAULT_MODELS_DIR = Path.home() / ".memnc" / "models"


class ModelTransport:
    def __init__(
        self,
        nc: Any,
        models_dir: Path | None = None,
        node: str = "unknown",
    ):
        self.nc = nc
        self.js: JetStreamContext = nc.jetstream()
        self.models_dir = models_dir or DEFAULT_MODELS_DIR
        self.node = node

    async def _store(self):
        try:
            return await self.js.object_store(OBJECT_STORE_BUCKET)
        except Exception:  # noqa: BLE001 - bucket missing on first offer
            return await self.js.create_object_store(OBJECT_STORE_BUCKET)

    async def _kv(self):
        try:
            return await self.js.key_value(KV_MANIFEST)
        except Exception:  # noqa: BLE001
            return await self.js.create_key_value(bucket=KV_MANIFEST)

    async def offer_model(self, path: str | Path) -> dict[str, Any]:
        """Upload one GGUF and publish its manifest. Returns the manifest."""
        path = Path(path)
        # Pre-hash (streamed) — nats-py needs a plain file-like for put.
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            while chunk := fh.read(MORSEL):
                digest.update(chunk)
        store = await self._store()
        with path.open("rb") as fh:
            await store.put(path.name, fh)
        manifest = {
            "name": path.name,
            "sha256": digest.hexdigest(),
            "size_mb": round(path.stat().st_size / 2**20, 1),
            "node_sources": [self.node],
        }
        kv = await self._kv()
        await kv.put(path.name, json.dumps(manifest).encode())
        log.info("model offered: %s (%s MB)", manifest["name"], manifest["size_mb"])
        return manifest

    async def get_manifest(self, name: str) -> dict[str, Any] | None:
        kv = await self._kv()
        try:
            entry = await kv.get(name)
        except KeyNotFoundError:  # noqa: F821 - nats.js.errors
            return None
        if entry is None or entry.value is None:
            return None
        return json.loads(entry.value)

    async def fetch_model(self, name: str) -> Path:
        """Download + verify one model into models_dir. Returns the path."""
        manifest = await self.get_manifest(name)
        if manifest is None:
            raise FileNotFoundError(f"no manifest for {name}")
        store = await self._store()
        obs = await store.get(name)
        data = obs.data
        digest = hashlib.sha256(data).hexdigest()
        if digest != manifest["sha256"]:
            raise ValueError(
                f"sha256 mismatch for {name}: {digest} != {manifest['sha256']}"
            )
        self.models_dir.mkdir(parents=True, exist_ok=True)
        out = _model_path(self.models_dir, name)
        out.write_bytes(data)
        log.info("model fetched: %s → %s", name, out)
        return out

    async def ensure_local(self, name: str) -> Path | None:
        """Model present locally? Then nothing to do, else fetch from mesh."""
        local = _model_path(self.models_dir, name)
        if local.exists():
            return local
        try:
            return await self.fetch_model(name)
        except FileNotFoundError:
            return None
