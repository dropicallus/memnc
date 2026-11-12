#!/usr/bin/env python3
"""memnc.discuss.common — shared settings and helpers for the discussion harness. shared settings and helpers for the discussion harness.

Single source of truth for NATS connection settings, roster discovery and
subject naming; used by memnc.discuss.agent and memnc.discuss.driver.
"""

import os


class Settings:
    """Discussion-harness settings, loaded from the environment."""

    def __init__(self) -> None:
        self.nats_url = os.environ.get("NATS_URL", "nats://127.0.0.1:4222")
        self.nats_token = os.environ.get("NATS_TOKEN", "dev-token")
        self.subject_fmt = os.environ.get("DISCUSS_SUBJECT_FMT", "discuss.{node}")
        self.roster_bucket = os.environ.get("DISCUSS_ROSTER_BUCKET", "roster")
        self.roster_prefix = os.environ.get("DISCUSS_ROSTER_PREFIX", "roster.")

    def node_subject(self, node_id: str) -> str:
        """Discussion subject for a roster node."""
        return self.subject_fmt.format(node=node_id)


def subject_from_node(settings: Settings, subject: str) -> str | None:
    """Inverse of node_subject: roster node id for a discussion subject."""
    prefix = settings.subject_fmt.split("{node}")[0]
    return subject.removeprefix(prefix) if subject.startswith(prefix) else None


async def roster_nodes(js, settings: Settings) -> list[str]:
    """Live, unpaused roster node ids (best-effort; [] if no roster KV)."""
    import json

    try:
        kv = await js.key_value(settings.roster_bucket)
        keys = await kv.keys()
    except Exception:
        return []
    out = []
    for key in sorted(keys):
        if not key.startswith(settings.roster_prefix):
            continue
        node_id = key[len(settings.roster_prefix) :]
        try:
            info = json.loads((await kv.get(key)).value.decode())
        except Exception:
            continue
        if not info.get("paused"):
            out.append(node_id)
    return out
