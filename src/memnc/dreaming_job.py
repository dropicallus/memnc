"""14.13 Step 6 build step 2: the dreaming job wrapper.

Wires the pure-logic core (``memnc.dreaming``) to the live mesh:
connects to NATS, applies the two safety gates from the roadmap design
(skip-if-busy and the single-writer ``dream.lock`` KV claim), reads the
deposited observations, and either prints the consolidation bullets
(dry-run, the default) or merges them into the experience store (--apply).

Run via ``python -m memnc.dreaming --dry-run|--apply`` (delegated from
``dreaming.py``'s ``__main__`` block). Dormant by design: refuses to run
unless ``Settings.dreaming_enabled`` is set by the operator.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

from .bus import Bus
from .config import Settings, get_settings
from .dreaming import dream_bullets
from .schema import TERMINAL_STATES

DREAM_LOCK_BUCKET = "dream_lock"
DREAM_LOCK_KEY = "dream.lock"
# A claim older than this is treated as abandoned (stale-claim reclaim,
# backlog-style). The job itself runs in well under a minute.
CLAIM_STALE_S = 3600


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None


async def _ensure_lock_kv(bus: Bus):
    from nats.js.errors import NotFoundError

    kv = None
    try:
        kv = await bus.js.key_value(DREAM_LOCK_BUCKET)
    except NotFoundError:
        kv = await bus.js.create_key_value(bucket=DREAM_LOCK_BUCKET, history=1)
    return kv


async def _claim_lock(kv) -> bool:
    """Single-writer guard: create the key or reclaim it once stale.

    Failure classes are kept broad on purpose: a CAS conflict or a lost
    race against another reclaimer must land in "skip", never crash the
    nightly job (real nats-py raises BadRequestError, the fake raises
    RuntimeError — both mean "someone else holds it").
    """
    now = _now_iso()
    try:
        await kv.create(DREAM_LOCK_KEY, now.encode())
        return True
    except Exception:  # noqa: BLE001 - key exists
        pass
    try:
        entry = await kv.get(DREAM_LOCK_KEY)
    except Exception:  # noqa: BLE001 - vanished between create and get
        try:
            await kv.create(DREAM_LOCK_KEY, now.encode())
            return True
        except Exception:  # noqa: BLE001 - raced
            return False
    claimed_at = _parse_iso(entry.value.decode(errors="replace").strip())
    if claimed_at is None:
        return False  # unparseable: refuse, never steal blindly
    age = (datetime.now(UTC) - claimed_at).total_seconds()
    if age <= CLAIM_STALE_S:
        return False
    try:
        await kv.update(DREAM_LOCK_KEY, now.encode(), last=entry.revision)
    except Exception:  # noqa: BLE001 - raced by another reclaimer
        return False
    return True


async def run_job(settings: Settings, apply: bool, bus: Bus | None = None) -> int:
    """Full dreaming pass. Returns a process exit code.

    ``bus`` is injectable for tests; when omitted a real Bus is created.
    """
    from memnc.experience_store import ExperienceStore, exclusive

    if not settings.dreaming_enabled:
        print(
            "dreaming job is disabled (Settings.dreaming_enabled=false) — "
            "dormant by design until the operator enables it.",
            file=sys.stderr,
        )
        return 2

    own_bus = bus is None
    if bus is None:
        bus = Bus(settings)
        await bus.connect()
    assert bus is not None  # for the type checker
    try:
        # Gate 1 — skip-if-busy: never race the CAS layer. Any task that
        # is not terminal means the mesh is mid-flight; skip (not an error).
        snapshots = await bus.list_tasks()
        active = [s for s in snapshots if s.state not in TERMINAL_STATES]
        if active:
            print(
                f"skipping dream: {len(active)} task(s) not terminal "
                "(skip-if-busy gate); rerun when the mesh is idle."
            )
            return 0

        # Gate 2 — single-writer claim.
        kv = await _ensure_lock_kv(bus)
        if not await _claim_lock(kv):
            print("skipping dream: another dream job holds the lock (or an unparseable claim).")
            return 0
        try:
            observations = await bus.list_observations()
            bullets = dream_bullets(observations)
            if not bullets:
                print("dream: nothing to consolidate (no eligible observations).")
                return 0
            for topic, texts in sorted(bullets.items()):
                for text in texts:
                    print(f"[{topic}] {text}")
            if not apply:
                print(f"dry-run: {sum(len(v) for v in bullets.values())} bullet(s) shown, store untouched.")
                return 0
            with exclusive(settings.experience_store_path):
                store = ExperienceStore(settings.experience_store_path).load()
                reports = {
                    topic: store.merge(topic, texts, source="dreaming-job")
                    for topic, texts in bullets.items()
                }
                store.save()
            for topic, report in sorted(reports.items()):
                print(
                    f"merged [{topic}]: {len(report.added)} added, "
                    f"{len(report.duplicates)} duplicate"
                    + (f", rejected: {report.rejected_reason}" if report.rejected_reason else "")
                )
            return 0
        finally:
            try:
                await kv.delete(DREAM_LOCK_KEY)
            except Exception:  # noqa: BLE001 - lock release is best-effort
                pass
    finally:
        if own_bus:
            await bus.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m memnc.dreaming",
        description="14.13 Step 6 dreaming job (nightly consolidation).",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="print bullets only (default)")
    mode.add_argument("--apply", action="store_true", help="merge bullets into the experience store")
    args = parser.parse_args(argv)
    settings: Settings = get_settings()
    import asyncio

    return asyncio.run(run_job(settings, apply=args.apply))


if __name__ == "__main__":  # pragma: no cover - direct invocation
    raise SystemExit(main())
