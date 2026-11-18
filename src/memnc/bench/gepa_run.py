"""CLI driver for a live GEPA prompt-evolution run (15.3 item 4).

Harvests failures from a bench JSONL, evolves one role's system prompt
(mutation from failures, crossover once the frontier splits), evaluates each
candidate by re-running the bench arm with ONLY that role's prompt swapped,
and writes the final Pareto frontier to a JSONL file. Zero weight changes —
costs rollouts only. Evaluation must run serially: candidates share the
process-wide SYSTEM_PROMPTS table.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from memnc.bench.gepa import evolve, load_failures
from memnc.bench.runner import BenchEngine
from memnc.bench.tasks import load_tasks
from memnc.roles import Role

log = logging.getLogger(__name__)


def load_seed_prompt(path: Path, role_name: str) -> str:
    """Return the highest-wins candidate prompt for ``role_name`` from a frontier JSONL."""
    best: dict | None = None
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            cand = json.loads(line)
            if cand.get("role") == role_name and (
                best is None or cand.get("wins", 0) > best.get("wins", 0)
            ):
                best = cand
    if best is None:
        raise SystemExit(f"no {role_name} candidate in {path}")
    log.info(
        "seeding from %s (wins %s/%s)",
        path,
        best.get("wins"),
        len(best.get("scores", [])),
    )
    return str(best["prompt"])


async def run_evolution(
    arm: str,
    role_name: str,
    tier: str,
    jsonl: Path,
    out_path: Path,
    generations: int = 3,
    engine: BenchEngine | None = None,
    ckpt_path: Path | None = None,
    seed_from: Path | None = None,
) -> list[dict[str, object]]:
    """Run one evolution and persist the Pareto frontier. Returns candidates."""
    failures = load_failures(jsonl, arm)
    if not failures:
        log.info("no failures for arm %s in %s; nothing to evolve", arm, jsonl)
        return []

    tasks = {t.id: t for t in load_tasks(tier)}
    failing_ids = sorted({f.task_id for f in failures if f.task_id in tasks})
    if not failing_ids:
        log.warning("failure task ids not found in tier %s tasks", tier)
        return []
    eval_tasks = [tasks[tid] for tid in failing_ids]

    own_engine = engine is None
    engine = engine or BenchEngine()
    reflect_model = engine._model(Role.CRITIC)

    async def reflect_chat(system: str, user: str, _model: str, temperature: float):
        return await engine._chat(system, user, reflect_model, temperature)

    base_prompt = None
    from memnc.roles import SYSTEM_PROMPTS

    if seed_from is not None:
        base_prompt = load_seed_prompt(seed_from, role_name)
    if base_prompt is None:
        base_prompt = SYSTEM_PROMPTS[Role(role_name)]
    frontier = await evolve(
        engine,
        reflect_chat,
        role_name,
        arm,
        base_prompt,
        failures,
        generations=generations,
        eval_tasks=eval_tasks,
        ckpt_path=ckpt_path,
    )
    if own_engine:
        await engine.close()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for cand in frontier:
            fh.write(
                json.dumps(
                    {
                        "role": cand.role,
                        "gen": cand.gen,
                        "prompt": cand.prompt,
                        "scores": dict(cand.scores),
                        "wins": sum(bool(v) for v in cand.scores.values()),
                    }
                )
                + "\n"
            )
    log.info("wrote %d frontier candidates to %s", len(frontier), out_path)
    return [
        {
            "role": c.role,
            "gen": c.gen,
            "prompt": c.prompt,
            "scores": dict(c.scores),
            "wins": sum(bool(v) for v in c.scores.values()),
        }
        for c in frontier
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GEPA live evolution run")
    parser.add_argument("--arm", default="B", help="bench arm whose failures seed evolution")
    parser.add_argument("--role", default="coder", help="role whose prompt evolves")
    parser.add_argument("--tier", default="hard")
    parser.add_argument("--jsonl", required=True, help="bench results JSONL to harvest")
    parser.add_argument("--out", required=True, help="frontier output JSONL")
    parser.add_argument("--generations", type=int, default=3)
    parser.add_argument(
        "--ckpt",
        default=None,
        help="incremental checkpoint JSONL (default: <out>.ckpt.jsonl); "
        "existing records are resumed, not recomputed",
    )
    parser.add_argument(
        "--seed-from",
        default=None,
        help="frontier JSONL from a previous run; its best candidate seeds generation 0",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    frontier = asyncio.run(
        run_evolution(
            args.arm,
            args.role,
            args.tier,
            Path(args.jsonl),
            Path(args.out),
            generations=args.generations,
            ckpt_path=Path(args.ckpt) if args.ckpt else Path(args.out + ".ckpt.jsonl"),
            seed_from=Path(args.seed_from) if args.seed_from else None,
        )
    )
    for cand in frontier:
        print(f"gen {cand['gen']} wins {cand['wins']}/{len(cand['scores'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
