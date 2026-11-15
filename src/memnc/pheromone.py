"""Pheromone routing (14.15.10): ACO-style strategy scores per task type.

Ant-colony optimization adapted to arm/strategy selection: every finished
task deposits "pheromone" on the edge (strategy) that served it. Choosing
a strategy for a task of a given type combines the accumulated pheromone
with a static cost prior:

    score(edge) = tau^ALPHA * eta^BETA,  tau = TAU0 + deposits,  eta = 1/ln(1 + tokens)

ALPHA weights learned success, BETA weights the cost prior. The numeric
defaults (TAU0, ALPHA, BETA) and the seed deposits below are initialized
from the measured 40-task hard-tier full run of 2026-11-15 (5 reps per
(task, arm) cell): the rescue matrix A 32/40, B 38/40, C 35/40 and the
per-cell mean tokens are frozen into the seed so routing starts from
evidence instead of a flat prior.

Escalation keeps the A -> B -> C rescue chain as a hard floor: whenever
an edge fails verification, the next edge in the chain is the minimum
candidate; pheromone may only raise an edge above the floor, never pick
a weaker edge after a failure.

The store is a plain KV bucket (one key per (task_type, edge), CAS-free
fold-on-write deposits). Deposits are a side channel, mirroring the
observation deposits: a broken store must never fail the task lifecycle.
"""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict

from pydantic import BaseModel

PHEROMONE_BUCKET = "mesh_pheromones"

TAU0 = 1.0
ALPHA = 1.0
BETA = 2.0

# Fraction of accumulated pheromone that evaporates per decay interval
# (orchestrator `_pheromone_loop`, every settings.pheromone_decay_interval_s).
RHO = 0.1

# Live tasks run the fixed planner->coder->critic pipeline; deposits for
# real mesh traffic land on this single edge until more strategies exist.
LIVE_EDGE = "mesh"

# Escalation floor: single-shot -> self-reflect -> mesh (arm A -> B -> C).
ESCALATION_CHAIN: tuple[str, ...] = ("A", "B", "C")

# Seed deposits from the 2026-11-15 hard-tier full run: (task_type, edge)
# -> (successes, reps, mean tokens). task_type is the bench task id, so a
# live task of the same kind routes against real measurements, not priors.
SEED_RUN: tuple[tuple[str, str, int, int, int], ...] = (
    ("hard_bst", "A", 5, 5, 741),
    ("hard_bst", "B", 5, 5, 1648),
    ("hard_bst", "C", 5, 5, 3421),
    ("hard_dijkstra", "A", 3, 5, 641),
    ("hard_dijkstra", "B", 5, 5, 2979),
    ("hard_dijkstra", "C", 4, 5, 5895),
    ("hard_longest_substr", "A", 5, 5, 478),
    ("hard_longest_substr", "B", 5, 5, 950),
    ("hard_longest_substr", "C", 5, 5, 3770),
    ("hard_matrix_rotate", "A", 5, 5, 457),
    ("hard_matrix_rotate", "B", 4, 5, 3557),
    ("hard_matrix_rotate", "C", 5, 5, 4114),
    ("hard_rate_limiter", "A", 0, 5, 572),
    ("hard_rate_limiter", "B", 4, 5, 1963),
    ("hard_rate_limiter", "C", 1, 5, 3830),
    ("hard_tokenizer", "A", 5, 5, 670),
    ("hard_tokenizer", "B", 5, 5, 1387),
    ("hard_tokenizer", "C", 5, 5, 4672),
    ("hard_topo_sort", "A", 5, 5, 721),
    ("hard_topo_sort", "B", 5, 5, 1576),
    ("hard_topo_sort", "C", 5, 5, 3884),
    ("hard_word_ladder", "A", 5, 5, 589),
    ("hard_word_ladder", "B", 5, 5, 1224),
    ("hard_word_ladder", "C", 5, 5, 4772),
)

_KEY_BAD = re.compile(r"[^a-zA-Z0-9._-]")


def edge_key(task_type: str, edge: str) -> str:
    """KV key for one (task_type, edge) cell; chars sanitized for NATS KV."""
    safe = _KEY_BAD.sub(".", f"{task_type}.{edge}")
    return safe


class PheromoneEntry(BaseModel):
    """Deposit accumulator for one (task_type, edge).

    Counters are floats: evaporation scales them down by (1 - rho) per
    decay interval, so fractional values are the normal steady state.
    """

    successes: float = 0.0
    failures: float = 0.0
    unsure: float = 0.0
    tokens: float = 0
    # static cost prior measured for this edge (mean tokens per run); 0
    # means "no measurement yet" and eta falls back to the deposit sum.
    cost_tokens: int = 0

    def tau(self) -> float:
        """Pheromone: seed + successful deposits (+ unsure as weak signal)."""
        return TAU0 + self.successes + self.unsure

    def eta(self) -> float:
        """Cost prior: cheaper edge scores higher; 1/ln dampens scale."""
        tokens = self.cost_tokens or (self.tokens // max(self.successes + self.failures, 1))
        return 1.0 / math.log(max(tokens, 2) + 1)

    def score(self) -> float:
        return self.tau() ** ALPHA * self.eta() ** BETA

    def deposit(self, outcome: str, tokens: int) -> None:
        """Fold one run outcome; outcome in {success, failure, unsure}."""
        if outcome == "success":
            self.successes += 1
        elif outcome == "unsure":
            self.unsure += 1
        else:
            self.failures += 1
        self.tokens += max(tokens, 0)

    def evaporate(self, rho: float) -> bool:
        """Evaporate accumulated pheromone toward tau0 by factor (1 - rho).

        ACO evaporation: stale evidence fades so the field tracks recent
        behaviour. cost_tokens (the measured static prior) is NOT decayed —
        it is a measurement of the edge, not accumulated belief. Returns
        True when any counter changed materially.
        """
        factor = 1.0 - rho
        before = (self.successes, self.unsure, self.failures, self.tokens)
        self.successes = round(self.successes * factor, 6)
        self.unsure = round(self.unsure * factor, 6)
        self.failures = round(self.failures * factor, 6)
        self.tokens = round(self.tokens * factor, 6)
        changed = before != (self.successes, self.unsure, self.failures, self.tokens)
        return changed


def seed_entries() -> dict[str, PheromoneEntry]:
    """Initial pheromone field from the measured full run."""
    entries: dict[str, PheromoneEntry] = {}
    for task_type, edge, successes, reps, tokens in SEED_RUN:
        entry = entries.setdefault(edge_key(task_type, edge), PheromoneEntry())
        for _ in range(successes):
            entry.deposit("success", 0)
        for _ in range(reps - successes):
            entry.deposit("failure", 0)
        entry.cost_tokens = tokens
    return entries


def choose_edge(entries: dict[str, PheromoneEntry], task_type: str, failed_edge: str | None = None) -> str | None:
    """Pick the highest-scoring edge for a task type.

    failed_edge (the strategy that just failed verification) is removed
    from the candidates so pheromone can never re-pick a known failure;
    the escalation chain supplies the fallback order instead.
    """
    candidates = {
        edge: entry
        for key, entry in entries.items()
        if key.startswith(f"{edge_key(task_type, '')}")
        for edge in [key[len(task_type) + 1 :]]
        if edge != failed_edge
    }
    if not candidates:
        return None
    return max(candidates, key=lambda edge: candidates[edge].score())


def escalate(current: str) -> str:
    """Next link on the rescue chain (A -> B -> C); stays at the end."""
    if current not in ESCALATION_CHAIN:
        return ESCALATION_CHAIN[0]
    idx = ESCALATION_CHAIN.index(current)
    return ESCALATION_CHAIN[min(idx + 1, len(ESCALATION_CHAIN) - 1)]


def format_pheromones(entries: dict[str, PheromoneEntry], task_type: str | None = None) -> str:
    """Human view for meshctl: tau, cost prior, score per edge cell."""
    keys = sorted(entries)
    if task_type:
        keys = [k for k in keys if k.startswith(f"{task_type}.")]
    if not keys:
        return "no pheromone deposits"
    lines = ["task_type.edge              tau  cost  score  (S/F/U)"]
    for key in keys:
        e = entries[key]
        lines.append(
            f"{key:28s} {e.tau():5.1f} {e.cost_tokens or '-':>5} {e.score():6.2f}  "
            f"({e.successes}/{e.failures}/{e.unsure})"
        )
    return "\n".join(lines)


class PheromoneStore:
    """KV-backed pheromone field (fold-on-write deposits, no CAS)."""

    def __init__(self, js) -> None:
        self.js = js
        self.kv = None

    async def ensure(self) -> None:
        from nats.js.errors import NotFoundError

        try:
            self.kv = await self.js.key_value(PHEROMONE_BUCKET)
        except NotFoundError:
            from nats.js.api import KeyValueConfig

            self.kv = await self.js.create_key_value(
                KeyValueConfig(bucket=PHEROMONE_BUCKET, history=10)
            )

    async def get(self, task_type: str, edge: str) -> PheromoneEntry:
        assert self.kv is not None
        from nats.js.errors import NotFoundError

        try:
            raw = await self.kv.get(edge_key(task_type, edge))
            return PheromoneEntry.model_validate(json.loads(raw.value))
        except NotFoundError:
            return PheromoneEntry()

    async def deposit(self, task_type: str, edge: str, outcome: str, tokens: int = 0) -> None:
        assert self.kv is not None
        entry = await self.get(task_type, edge)
        entry.deposit(outcome, tokens)
        await self.kv.put(edge_key(task_type, edge), entry.model_dump_json().encode())

    async def decay(self, rho: float = RHO) -> int:
        """Evaporate every cell; returns the number of cells rewritten."""
        assert self.kv is not None
        updated = 0
        for key in list(await self.kv.keys() or []):
            try:
                raw = await self.kv.get(key)
                entry = PheromoneEntry.model_validate(json.loads(raw.value))
            except Exception:  # noqa: BLE001 - skip unreadable cells
                continue
            if not entry.evaporate(rho):
                continue
            await self.kv.put(key, entry.model_dump_json().encode())
            updated += 1
        return updated

    async def seed(self) -> int:
        """Load the measured seed run once (skip cells already present)."""
        assert self.kv is not None
        loaded = 0
        for task_type, edge, successes, reps, tokens in SEED_RUN:
            key = edge_key(task_type, edge)
            try:
                await self.kv.get(key)
                continue
            except Exception:  # noqa: BLE001 - NotFoundError path, seed it
                pass
            entry = PheromoneEntry()
            for _ in range(successes):
                entry.deposit("success", 0)
            for _ in range(reps - successes):
                entry.deposit("failure", 0)
            entry.cost_tokens = tokens
            await self.kv.put(key, entry.model_dump_json().encode())
            loaded += 1
        return loaded

    async def entries(self) -> dict[str, PheromoneEntry]:
        assert self.kv is not None
        out: dict[str, PheromoneEntry] = {}
        try:
            keys = await self.kv.keys()
        except Exception:  # noqa: BLE001 - empty bucket
            return out
        for key in keys:
            try:
                raw = await self.kv.get(key)
                out[key] = PheromoneEntry.model_validate(json.loads(raw.value))
            except Exception:  # noqa: BLE001 - skip unreadable cells
                continue
        return out


def entries_by_task(entries: dict[str, PheromoneEntry]) -> dict[str, dict[str, PheromoneEntry]]:
    """Group flat keys back into {task_type: {edge: entry}}."""
    grouped: dict[str, dict[str, PheromoneEntry]] = defaultdict(dict)
    for key, entry in entries.items():
        task_type, _, edge = key.partition(".")
        grouped[task_type][edge] = entry
    return grouped


def task_type_from_prompt(task: str, max_chars: int = 40) -> str:
    """Stable task-type slug from the prompt head (live deposits)."""
    slug = re.sub("-{2,}", "-", _KEY_BAD.sub("-", task.lower()))[:max_chars].strip("-")
    return slug or "live"
