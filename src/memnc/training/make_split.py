"""Set up the two-day EWC test: split a traces JSONL into tag1/tag2.

Usage:
  python -m memnc.training.make_split --in training_data.jsonl --out-dir ./splits

Creates ./splits/tag1.jsonl and ./splits/tag2.jsonl (halves, shuffled)
without destroying the usage distribution. Deterministic order (fixed seed).
"""

from __future__ import annotations

import argparse
import json
import os
import random


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out-dir", default="splits")
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()

    with open(a.inp, encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]
    rng = random.Random(a.seed)
    rng.shuffle(rows)
    n = len(rows) // 2
    tag1, tag2 = rows[:n], rows[n:]
    os.makedirs(a.out_dir, exist_ok=True)
    for name, items in [("tag1.jsonl", tag1), ("tag2.jsonl", tag2)]:
        p = os.path.join(a.out_dir, name)
        with open(p, "w", encoding="utf-8") as f:
            for r in items:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"{name}: {len(items)} samples -> {p}")
    print(
        "\nUsage:\n"
        f"  Tag1: python -m memnc.training.train_ewc --data "
        f"{os.path.join(a.out_dir, 'tag1.jsonl')} --ewc-lambda 0 --memory-dir ./memory_twoday\n"
        f"  Tag2: python -m memnc.training.train_ewc --data "
        f"{os.path.join(a.out_dir, 'tag2.jsonl')} --ewc-lambda 1000 --memory-dir ./memory_twoday"
    )


if __name__ == "__main__":
    main()
