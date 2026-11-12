#!/usr/bin/env python3
"""memnc.discuss.driver — mediates a turn-based discussion over NATS (dynamic N). mediates a turn-based discussion over NATS (dynamic N).

Participants are discovered from the mesh roster KV — the same discovery the
orchestrator uses (see memnc.discuss.common.roster_nodes). Only live, unpaused
nodes participate. Participants can be pinned via DISCUSS_NODES
(comma-separated node ids) or DISCUSS_SUBJECTS (subjects for nodes without a
roster entry).

Usage: memnc.discuss.driver <topic> [rounds] [timeout_s] [n_agents]
Env: NATS_URL, NATS_TOKEN, DISCUSS_NODES, DISCUSS_SUBJECTS,
     DISCUSS_SUBJECT_FMT (default "discuss.{node}")
Saves transcript to discussions/disc_<ts>.txt
"""

import asyncio
import json
import os
import sys
import time

import nats

from memnc.discuss.common import Settings, roster_nodes

S = Settings()


async def ask(nc, subj, name, topic, transcript, timeout):
    payload = json.dumps({"topic": topic, "transcript": transcript}).encode()
    t0 = time.time()
    try:
        m = await nc.request(subj, payload, timeout=timeout)
        r = json.loads(m.data.decode())
        r["latency_s"] = round(time.time() - t0, 1)
        return r
    except Exception as e:
        return {
            "agent": name,
            "text": f"[NO ANSWER: {e!r}]",
            "done": True,
            "latency_s": round(time.time() - t0, 1),
        }


async def main():
    topic = sys.argv[1] if len(sys.argv) > 1 else "AI agent cooperation"
    rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    timeout = float(sys.argv[3]) if len(sys.argv) > 3 else 620
    n_want = int(sys.argv[4]) if len(sys.argv) > 4 else 2

    nc = await nats.connect(servers=[S.nats_url], token=S.nats_token, name="discuss-driver")
    js = nc.jetstream()

    # Participant discovery: pinned subjects > pinned nodes > live roster.
    if os.environ.get("DISCUSS_SUBJECTS"):
        subjects = [s.strip() for s in os.environ["DISCUSS_SUBJECTS"].split(",") if s.strip()]
        names = subjects
    elif os.environ.get("DISCUSS_NODES"):
        names = [n.strip() for n in os.environ["DISCUSS_NODES"].split(",") if n.strip()]
        subjects = [S.node_subject(n) for n in names]
    else:
        subjects = [S.node_subject(n) for n in (await roster_nodes(js, S))[:n_want]]
        names = subjects
    if not subjects:
        print("no participants — start workers or set DISCUSS_NODES/DISCUSS_SUBJECTS")
        return

    transcript = []
    out = [f"=== DISCUSSION: {topic} ===", f"{', '.join(names)} — max {rounds} rounds", ""]

    done_by = None
    for rnd in range(1, rounds + 1):
        for subj, name in zip(subjects, names, strict=True):
            r = await ask(nc, subj, name, topic, transcript, timeout)
            text = r["text"].strip()
            transcript.append({"speaker": name, "text": text})
            out += [f"--- round {rnd} | {name} ({r.get('latency_s')}s) ---", text, ""]
            print(f"[R{rnd}] {name}: {text[:120]}...", flush=True)
            if r.get("done") and rnd >= 2:
                done_by = name
                break
        if done_by:
            out.append(f"[ENDED by {done_by}, round {rnd}]")
            print("DONE", flush=True)
            break
    else:
        out.append("[MAX ROUNDS REACHED]")
        print("MAXROUNDS", flush=True)

    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "discussions")
    out_path = os.path.join(out_dir, f"disc_{time.strftime('%Y%m%d_%H%M%S')}.txt")
    os.makedirs(out_dir, exist_ok=True)
    with open(out_path, "w") as f:
        f.write("\n".join(out))
    print("transcript:", out_path, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
