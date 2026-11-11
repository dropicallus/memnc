"""Split traces by domain into tag1/tag2 (domain-based, not random).
Filters out the Open-WebUI '### Task:' meta-calls (no training on them).

Usage:
  python -m memnc.training.make_domain_split --db traces.db --out-dir ./splits_dom
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3

# Keyword heuristics for domain detection: list A matches day-1 style chat,
# list B matches the other-language (Hindi) requests.
DOMAIN_A_KEYS = [
    "build", "ascii", "monkey", "ping", "pong", "search", "internet",
    "how", "what", "weather", "berlin", "program", "poem", "get started",
    "shoes", "blue", "buy", "test", "recognize", "realistic", "more characters",
]
DOMAIN_B_KEYS = [
    "hindi", "yogi", "master", "story", "translate", "proverb",
    "hindi language", "classic", "song", "woman", "information which women",
    "ask", "indian",
]


def detect_domain(text: str):
    t = (text or "").lower()
    b = sum(1 for k in DOMAIN_B_KEYS if k in t)
    a = sum(1 for k in DOMAIN_A_KEYS if k in t)
    # Devanagari script marks the other-language domain (mainly in answers,
    # but also in prompts).
    if re.search(r"[\u0900-\u097F]", text or ""):
        b += 2
    if b > a:
        return "b"
    return "a"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="traces.db")
    ap.add_argument("--out-dir", default="splits_dom")
    a = ap.parse_args()
    c = sqlite3.connect(a.db)
    c.row_factory = sqlite3.Row

    dom_a, dom_b = [], []
    meta_skipped = 0
    for r in c.execute(
        "SELECT id, request_json, response_json FROM calls "
        "WHERE endpoint='/v1/chat/completions' AND status='success' ORDER BY id"
    ):
        req = json.loads(r["request_json"])
        msgs = req.get("messages", [])
        last_user = ""
        for m in reversed(msgs):
            if m.get("role") == "user":
                last_user = m.get("content") or ""
                break
        if last_user.startswith("### Task:"):
            meta_skipped += 1
            continue
        resp = json.loads(r["response_json"]) if r["response_json"] else {}
        choice = resp.get("choices", [{}])[0].get("message", {})
        if not (choice.get("content") or choice.get("tool_calls")):
            continue
        sample = {
            "messages": msgs,
            "sources": {"call_id": r["id"], "domain": detect_domain(last_user)},
        }
        (dom_b if detect_domain(last_user) == "b" else dom_a).append(sample)

    os.makedirs(a.out_dir, exist_ok=True)
    for name, items in [("tag1_dom_a.jsonl", dom_a), ("tag2_dom_b.jsonl", dom_b)]:
        p = os.path.join(a.out_dir, name)
        with open(p, "w", encoding="utf-8") as f:
            for s in items:
                f.write(json.dumps(s, ensure_ascii=False) + "\n")
        print(f"{name}: {len(items)} samples -> {p}")
    print(f"Task meta-calls skipped: {meta_skipped}")
    print(f"\nTag1 = domain A ({len(dom_a)}), Tag2 = domain B ({len(dom_b)})")


if __name__ == "__main__":
    main()
