"""Re-run only failed (task, rep) combos of the arm-D hard bench (ReadTimeout reruns)."""
import asyncio, json, sys, time
from pathlib import Path
from dataclasses import asdict
from memnc.bench.runner import BenchEngine

# (task_id, rep) of the 9 ReadTimeout failures
FAILED = [
    ("hard_rate_limiter", 0), ("hard_rate_limiter", 2),
    ("hard_dijkstra", 0), ("hard_dijkstra", 1), ("hard_dijkstra", 2),
    ("hard_bst", 0),
    ("hard_word_ladder", 0), ("hard_word_ladder", 1),
    ("hard_tokenizer", 2),
]

async def main() -> None:
    import tempfile
    from memnc.bench.tasks import load_tasks
    from memnc.bench.runner import write_report
    tasks = load_tasks("hard")
    by_id = {t.id: t for t in tasks}
    suite = [{"arm": "D", "task": by_id[rid], "rep": rep} for rid, rep in FAILED]
    engine = BenchEngine()
    workroot = Path(tempfile.gettempdir()) / "memnc-bench" / time.strftime("%Y%m%d-%H%M%S")
    out_path = Path("workspace/bench-d-hard-retry.jsonl")
    results = []
    try:
        for i, spec in enumerate(suite, start=1):
            print(f"[{i}/{len(suite)}] arm D rep {spec['rep']} {spec['task'].id} ...", flush=True)
            row = await engine.run_task(spec["task"], spec["arm"], spec["rep"], workroot)
            results.append(row)
            with out_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(asdict(row)) + "\n")
            print(f"    -> {'PASS' if row.success else 'FAIL'} ({row.elapsed_s}s, {row.llm_calls} calls)", flush=True)
    finally:
        await engine.close()
        if results:
            write_report(results, Path("workspace/bench-d-hard-retry.md"))
            print(f"report written: workspace/bench-d-hard-retry.md", flush=True)
    n_pass = sum(1 for r in results if r.success)
    print(f"done: {n_pass}/{len(results)} passed", flush=True)

if __name__ == "__main__":
    asyncio.run(main())
