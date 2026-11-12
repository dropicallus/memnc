"""M11d: model-mediated API agent — natural language in, mesh API call out.

Flexibility contract: NOTHING about the model is hard-coded. Endpoint,
model name and the allowed verb set all come from Settings/env
(MESH_LLM_BASE_URL, MESH_LLM_MODEL, MESH_AGENT_VERBS), so the agent can
ride any OpenAI-compatible backend (local llauncher, trace-proxy,
LM Studio, cloud) without code changes.

Safety: the agent is an API *client*, not a writer of its own. It may
only call verbs listed in agent_allowed_verbs (default: reads +
task.submit); every mutating call still goes through the orchestrator's
mesh.control grant-check, so a hallucinated verb fails closed.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from .api import api_request
from .bus import Bus
from .config import Settings

log = logging.getLogger(__name__)

DEFAULT_VERBS = (
    "task.list,task.detail,node.list,node.detail,"
    "cap.list,grant.list,trust.list,ledger.read,task.submit"
)

SYSTEM_PROMPT = """\
You translate one natural-language question about a compute mesh into
exactly ONE mesh API call. Reply ONLY with minified JSON, no prose, no
markdown fences: {"verb": "...", "args": {...}}
Use {"verb": "", "args": {}} when the question is not an API action.

Verbs and their args:
- task.list   {state?, node?}      (state: RECEIVED|PLANNING|CODING|REVIEW|DONE|FAILED|CANCELLED)
- task.detail {task_id}
- node.list   {}
- node.detail {node_id}
- cap.list    {}
- grant.list  {}
- trust.list  {}
- ledger.read {}
- task.submit {task}              (task: the user's task text, verbatim)
"""


class ApiAgent:
    """Turn a NL question into one API verb call and a human answer."""

    def __init__(self, settings: Settings, bus: Bus):
        self.settings = settings
        self.bus = bus
        self.allowed: set[str] = {
            v.strip() for v in settings.agent_allowed_verbs.split(",") if v.strip()
        }

    async def ask(self, question: str) -> str:
        verb, args = await self._plan(question)
        if not verb:
            return "Das ist keine Mesh-Afrage — ich kann Tasks, Nodes, Caps, Grants, Trust und Ledger abfragen."
        if verb not in self.allowed:
            return f"verweigert: Verb {verb!r} ist für den Agenten nicht freigegeben"
        try:
            resp = await api_request(self.bus, verb, timeout=30, **args)
        except Exception as exc:  # noqa: BLE001 - surface the cause, never crash
            return f"Fehler bei {verb}: {exc}"
        return self._format(verb, resp["data"])

    # -- model call -----------------------------------------------------------

    async def _plan(self, question: str) -> tuple[str, dict[str, Any]]:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ]
        content = await self._chat(messages)
        return self._parse_plan(content)

    async def _chat(self, messages: list[dict[str, str]]) -> str:
        url = f"{self.settings.agent_llm_base_url.rstrip('/')}/chat/completions"
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                url,
                json={
                    "model": self.settings.agent_llm_model,
                    "messages": messages,
                    "temperature": 0,
                },
            )
            r.raise_for_status()
        return str(r.json()["choices"][0]["message"]["content"])

    @staticmethod
    def _parse_plan(content: str) -> tuple[str, dict[str, Any]]:
        text = content.strip()
        if text.startswith("```"):
            text = text.strip("`").lstrip("json").strip()
        try:
            plan = json.loads(text)
        except json.JSONDecodeError:
            return "", {}
        verb = str(plan.get("verb", ""))
        args = plan.get("args") or {}
        return verb, args if isinstance(args, dict) else {}

    # -- rendering ------------------------------------------------------------

    @staticmethod
    def _format(verb: str, data: Any) -> str:
        if verb == "task.list":
            if not data:
                return "Keine Tasks."
            rows = [
                f"#{t['task_id'][:12]} {t['state']} ({t.get('claimed_by', '') or 'unclaimed'})"
                for t in data
            ]
            return f"{len(rows)} Tasks:\n" + "\n".join(rows)
        if verb == "node.list":
            rows = [
                f"{n['node_id']}: roles={','.join(n['roles'])} load={n.get('load', 0):.0%}"
                f" models={','.join(n['models']) or '-'}"
                for n in data
            ]
            return "Nodes:\n" + ("\n".join(rows) or "-")
        if verb == "task.detail":
            return f"#{data['task_id'][:12]} {data['state']}: {data['task'][:200]}"
        if verb == "node.detail":
            node, tasks = data["node"], data["tasks"]
            head = (
                f"node {node['node_id']} roles={','.join(node['roles'])}"
                f" load={node.get('load', 0):.0%} vram={node.get('vram_gb', 0)}GB"
            )
            if not tasks:
                return head + " — keine Tasks"
            return head + "\n" + "\n".join(f"#{t['task_id'][:12]} {t['state']}" for t in tasks)
        if verb == "task.submit":
            return f"submitted: #{data.get('task_id')}"
        if verb in ("cap.list", "grant.list", "trust.list"):
            return json.dumps(data, indent=1)[:1500]
        if verb == "ledger.read":
            return json.dumps(data, indent=1)[:2000]
        return json.dumps(data, indent=1)[:1500]
