"""Lane 2: re-run failed (task, rep) combos against the llauncher/llama-server backend (OpenAI API)."""
import asyncio
import json
import time
from dataclasses import asdict
from pathlib import Path

from memnc.bench.runner import BenchEngine

FAILED = [
    ("hard_rate_limiter", 0), ("hard_rate_limiter", 2),
    ("hard_dijkstra", 0), ("hard_dijkstra", 1), ("hard_dijkstra", 2),
    ("hard_bst", 0),
    ("hard_word_ladder", 0), ("hard_word_ladder", 1),
    ("hard_tokenizer", 2),
]

# lane 2 splits: upper half of the list
MINE = FAILED[5:]  # hard_bst, hard_word_ladder x2, hard_tokenizer

async def main() -> None:
    import tempfile

    from memnc.bench.tasks import load_tasks
    tasks = load_tasks("hard")
    by_id = {t.id: t for t in tasks}
    suite = [{"arm": "D", "task": by_id[rid], "rep": rep} for rid, rep in MINE]
    engine = BenchEngine()
    workroot = Path(tempfile.gettempdir()) / "memnc-bench" / ("lane2-" + time.strftime("%Y%m%d-%H%M%S"))
    out_path = Path("workspace/bench-d-hard-retry-lane2.jsonl")
    n = 0
    try:
        for i, spec in enumerate(suite, start=1):
            print(f"[L2 {i}/{len(suite)}] rep {spec['rep']} {spec['task'].id} ...", flush=True)
            row = await engine.run_task(spec["task"], spec["arm"], spec["rep"], workroot)
            with out_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(asdict(row)) + "\n")
            print(f"    -> {'PASS' if row.success else 'FAIL'} ({row.elapsed_s}s, {row.llm_calls} calls)", flush=True)
            n += 1
    finally:
        await engine.close()
    print(f"lane2 done: {n}/{len(suite)}", flush=True)

if __name__ == "__main__":
    asyncio.run(main())
