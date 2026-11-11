#!/usr/bin/env python3
"""discuss_agent.py — NATS discussion responder (one per node).

Subscribes DISCUSS_SUBJECT (request/reply). Payload JSON:
  {"topic": str, "transcript": [ {"speaker": str, "text": str}, ... ]}
Replies JSON: {"agent": str, "text": str, "done": bool}

The transcript is rendered as real chat messages instead of one prompt blob:
the agent's own earlier turns become assistant messages, other speakers
become user messages. Speaker->role mapping is configurable via
DISCUSS_ROLE_OTHERS (comma list of speakers to render as tool-role context,
default none).

Env: DISCUSS_AGENT, DISCUSS_SUBJECT, DISCUSS_STANCE, DISCUSS_LANG,
     DISCUSS_MAX_WORDS, DISCUSS_ROLE_OTHERS, DISCUSS_DONE_MARK,
     OLLAMA_URL, OLLAMA_MODEL, OLLAMA_NUM_CTX
     (NATS settings shared via discuss_common.Settings)
"""
import asyncio, json, os, time, urllib.request

from discuss_common import Settings

S = Settings()

AGENT = os.environ.get("DISCUSS_AGENT", "agent")
SUBJECT = os.environ.get("DISCUSS_SUBJECT", "discuss.ping")
STANCE = os.environ.get("DISCUSS_STANCE", "")
LANG = os.environ.get("DISCUSS_LANG", "de")
MAX_WORDS = int(os.environ.get("DISCUSS_MAX_WORDS", "130"))
DONE_MARK = os.environ.get("DISCUSS_DONE_MARK", "[DONE]")
# speakers rendered as {"role": "tool"} context instead of user turns
ROLE_OTHERS = {s.strip() for s in os.environ.get("DISCUSS_ROLE_OTHERS", "").split(",") if s.strip()}
NATS_URL = S.nats_url
NATS_TOKEN = S.nats_token
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2:3b")
NUM_CTX = int(os.environ.get("OLLAMA_NUM_CTX", "16384"))

_SYSTEM = (
    f"You are '{AGENT}', an AI agent in a multi-agent discussion. "
    + (STANCE + " " if STANCE else "")
    + "Discuss factually, contribute your own points and respond concretely "
    "to the latest contributions. "
    f"Reply EXCLUSIVELY with your contribution, at most {MAX_WORDS} words, "
    f"in {LANG}, no preamble. "
    f"If you consider the topic sufficiently covered and have nothing "
    f"substantial left to add, append the mark {DONE_MARK} at the end of "
    "your contribution."
)


def build_messages(topic: str, transcript: list) -> list:
    """Transcript -> chat messages. Own turns = assistant, others = user
    (or tool context for speakers listed in DISCUSS_ROLE_OTHERS)."""
    messages = [{"role": "system", "content": _SYSTEM}]
    for entry in transcript:
        speaker = entry.get("speaker", "?")
        text = entry.get("text", "")
        if speaker == AGENT:
            messages.append({"role": "assistant", "content": text})
        elif speaker in ROLE_OTHERS:
            messages.append({"role": "tool", "tool_call_id": speaker,
                             "name": speaker, "content": text})
        else:
            messages.append({"role": "user", "content": f"{speaker}: {text}"})
    messages.append({"role": "user",
                     "content": f"TOPIC: {topic}\n\nYour contribution as {AGENT}:"})
    return messages


def call_ollama(messages: list) -> str:
    payload = json.dumps({
        "model": MODEL,
        "messages": messages,
        "stream": False,
        "options": {"temperature": 0.7, "num_ctx": NUM_CTX},
    }).encode()
    req = urllib.request.Request(OLLAMA_URL + "/api/chat", data=payload,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read())["message"]["content"].strip()


async def main():
    import nats
    nc = await nats.connect(servers=[NATS_URL], token=NATS_TOKEN,
                            name=f"discuss-{AGENT}")

    async def handler(msg):
        t0 = time.time()
        try:
            req = json.loads(msg.data.decode())
            messages = build_messages(req["topic"], req.get("transcript", []))
            text = call_ollama(messages)
            done = DONE_MARK in text
            resp = {"agent": AGENT, "text": text, "done": done,
                    "dur_s": round(time.time() - t0, 1)}
        except Exception as e:
            resp = {"agent": AGENT, "text": f"[ERROR: {e!r}]", "done": True,
                    "dur_s": round(time.time() - t0, 1)}
        await msg.respond(json.dumps(resp).encode())
        print(f"[{AGENT}] turn in {resp['dur_s']}s done={resp['done']}", flush=True)

    await nc.subscribe(SUBJECT, cb=handler)
    await nc.flush()
    print(f"[{AGENT}] ready on {SUBJECT} (model={MODEL})", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
