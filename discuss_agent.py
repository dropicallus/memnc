#!/usr/bin/env python3
"""discuss_agent.py — NATS discussion responder (one per node).

Subscribes DISCUSS_SUBJECT (request/reply). Payload JSON:
  {"topic": str, "transcript": [ {"speaker": str, "text": str}, ... ], "round": int}
Replies JSON: {"agent": str, "text": str, "done": bool}

Env: DISCUSS_AGENT, DISCUSS_SUBJECT, NATS_URL, NATS_TOKEN,
     OLLAMA_URL, OLLAMA_MODEL
"""
import asyncio, json, os, sys, time, urllib.request

AGENT = os.environ.get("DISCUSS_AGENT", "agent")
SUBJECT = os.environ.get("DISCUSS_SUBJECT", "discuss.ping")
NATS_URL = os.environ.get("NATS_URL", "nats://127.0.0.1:4222")
NATS_TOKEN = os.environ.get("NATS_TOKEN", "dev-token")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2:3b")
MAX_WORDS = int(os.environ.get("DISCUSS_MAX_WORDS", "130"))

SYSTEM = (
    f"Du bist '{AGENT}', ein KI-Agent in einer Diskussion mit einem anderen Agenten. "
    "Diskutiere sachlich, bring eigene Punkte und reagiere konkret auf den letzten Beitrag. "
    f"Antworte AUSSCHLIESSLICH mit deinem Beitrag, maximal {MAX_WORDS} Woerter, auf Deutsch, keine Einleitung. "
    "Wenn du findest, dass das Thema fruchtbar behandelt wurde und du nichts Wesentliches mehr beitragen kannst, "
    "haenge am Ende deines Beitrags die Marke [DONE] an."
)

def call_ollama(prompt: str) -> str:
    payload = json.dumps({
        "model": MODEL,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": prompt}],
        "stream": False,
        "options": {"temperature": 0.7, "num_ctx": int(os.environ.get("OLLAMA_NUM_CTX", "16384"))},
    }).encode()
    req = urllib.request.Request(OLLAMA_URL + "/api/chat", data=payload,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read())["message"]["content"].strip()

def build_prompt(topic: str, transcript: list) -> str:
    lines = [f"THEMA: {topic}", "", "DISKUSSION BISHER:"]
    for t in transcript:
        lines.append(f"{t['speaker']}: {t['text']}")
    lines += ["", f"Dein Beitrag als {AGENT}:"]
    return "\n".join(lines)

async def main():
    import nats
    nc = await nats.connect(servers=[NATS_URL], token=NATS_TOKEN,
                            name=f"discuss-{AGENT}")

    async def handler(msg):
        t0 = time.time()
        try:
            req = json.loads(msg.data.decode())
            prompt = build_prompt(req["topic"], req.get("transcript", []))
            text = call_ollama(prompt)
            done = "[DONE]" in text
            resp = {"agent": AGENT, "text": text, "done": done,
                    "dur_s": round(time.time() - t0, 1)}
        except Exception as e:
            resp = {"agent": AGENT, "text": f"[FEHLER: {e!r}]", "done": True,
                    "dur_s": round(time.time() - t0, 1)}
        await msg.respond(json.dumps(resp).encode())
        print(f"[{AGENT}] turn in {resp['dur_s']}s done={resp['done']}", flush=True)

    await nc.subscribe(SUBJECT, cb=handler)
    await nc.flush()
    print(f"[{AGENT}] ready on {SUBJECT} (model={MODEL})", flush=True)
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
