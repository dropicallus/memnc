#!/usr/bin/env python3
"""discuss_driver.py — mediates a turn-based discussion between two NATS agents.

Usage: discuss_driver.py <topic> [rounds] [timeout_s]
Env: NATS_URL, NATS_TOKEN, DISCUSS_A_SUBJECT, DISCUSS_B_SUBJECT, DISCUSS_A_NAME, DISCUSS_B_NAME
Saves transcript to discussions/disc_<ts>.txt
"""
import asyncio, json, os, sys, time
import nats

NATS_URL = os.environ.get("NATS_URL", "nats://127.0.0.1:4222")
NATS_TOKEN = os.environ.get("NATS_TOKEN", "dev-token")
A_SUBJ = os.environ.get("DISCUSS_A_SUBJECT", "discuss.node-b")
A_NAME = os.environ.get("DISCUSS_A_NAME", "node-b")
B_SUBJ = os.environ.get("DISCUSS_B_SUBJECT", "discuss.node-a")
B_NAME = os.environ.get("DISCUSS_B_NAME", "node-a")

async def ask(nc, subj, name, topic, transcript, timeout):
    payload = json.dumps({"topic": topic, "transcript": transcript}).encode()
    t0 = time.time()
    try:
        m = await nc.request(subj, payload, timeout=timeout)
        r = json.loads(m.data.decode())
        r["latency_s"] = round(time.time() - t0, 1)
        return r
    except Exception as e:
        return {"agent": name, "text": f"[KEINE ANTWORT: {e!r}]", "done": True,
                "latency_s": round(time.time() - t0, 1)}

async def main():
    topic = sys.argv[1] if len(sys.argv) > 1 else "KI-Agenten-Kooperation"
    rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    timeout = float(sys.argv[3]) if len(sys.argv) > 3 else 620

    nc = await nats.connect(servers=[NATS_URL], token=NATS_TOKEN, name="discuss-driver")
    transcript = []
    out = [f"=== DISKUSSION: {topic} ===", f"{A_NAME} vs {B_NAME}, max {rounds} Runden", ""]
    out_path = globals().get("out_path", f"discussions/disc_{time.strftime('%Y%m%d_%H%M%S')}.txt")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    for rnd in range(1, rounds + 1):
        for subj, name in ((A_SUBJ, A_NAME), (B_SUBJ, B_NAME)):
            r = await ask(nc, subj, name, topic, transcript, timeout)
            text = r["text"].strip()
            transcript.append({"speaker": name, "text": text})
            out.append(f"--- Runde {rnd} | {name} ({r.get('latency_s')}s) ---")
            out.append(text)
            out.append("")
            print(f"[R{rnd}] {name}: {text[:120]}...", flush=True)
            if r.get("done") and rnd >= 2:
                out.append(f"[BEENDET durch {name}, Runde {rnd}]")
                print("DONE", flush=True)
                break
        else:
            continue
        break
    else:
        out.append("[MAX RUNDEN ERREICHT]")
        print("MAXROUNDS", flush=True)
    with open(out_path, "w") as f:
        f.write("\n".join(out))
    print("transcript:", out_path, flush=True)

if __name__ == "__main__":
    ts = time.strftime("%Y%m%d_%H%M%S")
    os.makedirs("discussions", exist_ok=True)
    out_path = f"discussions/disc_{ts}.txt"
    globals()["out_path"] = out_path
    asyncio.run(main())
    print("transcript:", out_path, flush=True)
