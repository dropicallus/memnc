"""Cold-standby backup for the KV ledger (Duo topology).

The mesh state lives in JetStream KV buckets on the active NATS host.
A Raft cluster needs a majority, so a 2-host setup cannot give automatic
failover — the recovery story is a periodic dump + documented promote.
This module dumps every durable bucket into one JSON file and restores
it on a stand-by host. Ephemeral (TTL) buckets -- registry and roster --
carry presence only and are deliberately skipped: they repopulate
themselves within seconds of a node joining.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from .config import Settings
from .observations import OBSERVATIONS_BUCKET
from .schema import GRANT_BUCKET, REGISTRY_BUCKET, ROSTER_BUCKET, TRUST_BUCKET

log = logging.getLogger(__name__)

#: buckets that hold durable state (backed up and restored)
DURABLE_BUCKETS = (GRANT_BUCKET, OBSERVATIONS_BUCKET, TRUST_BUCKET)

#: presence-only buckets, never backed up (TTL repopulation is the design)
EPHEMERAL_BUCKETS = (REGISTRY_BUCKET, ROSTER_BUCKET)


def backup_bucket_names(settings: Settings) -> list[str]:
    """Bucket names a backup covers, in restore order (durable + main state last)."""
    return [*DURABLE_BUCKETS, settings.kv_bucket]


async def dump_buckets(bus) -> dict:
    """Read every durable KV bucket into a plain dict.

    Returns ``{"meta": {...}, "buckets": {name: {key: value}}}`` with
    values as UTF-8 strings (all memnc KV values are JSON documents).
    """
    assert bus.js is not None
    buckets: dict[str, dict[str, str]] = {}
    for name in backup_bucket_names(bus.settings):
        try:
            kv = await bus.js.key_value(name)
        except Exception:  # bucket absent on this deployment — skip
            log.info("backup: bucket %s absent, skipped", name)
            continue
        entries: dict[str, str] = {}
        for key in sorted(await kv.keys()):
            entry = await kv.get(key)
            entries[key] = entry.value.decode("utf-8") if entry.value else ""
        buckets[name] = entries
    return {
        "meta": {
            "created_at": datetime.now(UTC).isoformat(),
            "nats_url": bus.settings.nats_url,
            "kv_bucket": bus.settings.kv_bucket,
        },
        "buckets": buckets,
    }


async def restore_buckets(bus, data: dict, purge: bool = False) -> dict:
    """Write a dump back into the live KV buckets.

    Upsert semantics: existing keys are overwritten by the backup, keys
    that exist only on the live side are kept (this restores onto a fresh
    server as well as refreshes an older one). ``purge=True`` additionally
    deletes live keys that the backup does not contain, making the target
    an exact mirror. Returns per-bucket counts.
    """
    assert bus.js is not None
    counts: dict[str, int] = {}
    for name, entries in data.get("buckets", {}).items():
        try:
            kv = await bus.js.key_value(name)
        except Exception:
            log.warning("restore: bucket %s missing on target, skipped", name)
            continue
        live_keys: set[str] = set()
        if purge:
            try:
                live_keys = set(await kv.keys())
            except Exception:
                live_keys = set()
        for key, value in sorted(entries.items()):
            await kv.put(key, value.encode("utf-8"))
            live_keys.discard(key)
        for key in sorted(live_keys):
            await kv.delete(key)
        counts[name] = len(entries)
    return counts
