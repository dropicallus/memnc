"""Mesh-Console: local web view of the mesh (S3).

Stdlib-only HTTP server (no framework) that renders the same state as
``meshctl tasks`` / ``ledger`` / ``registry`` / ``grants`` / ``trust`` in a
browser. Every request opens its own short-lived NATS connection, so the
console stays stateless and poll-friendly.

T4: the console can also forward a small set of human control commands
(pause/resume a node, cancel/retry a task) via ``POST /api/control``. It
always acts as agent ``console`` — the orchestrator grant-checks that id,
so without ``meshctl grant console`` the buttons answer with a deny.
Reads stay read-only.

Run with:  python -m memnc.console [--port 8765]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .bus import Bus
from .cli import _connect
from .config import get_settings
from .schema import SUBJECT_CONTROL, TERMINAL_STATES, ControlCommand

_REFRESH_MS = 3000

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>memnc mesh console</title>
<style>
:root { color-scheme: dark; }
body { background:#0d1117; color:#c9d1d9; font:14px/1.5 ui-monospace,monospace;
       margin:0; padding:1rem 1.5rem; }
h1 { font-size:1.1rem; color:#58a6ff; margin:0 0 .2rem; }
h2 { font-size:.95rem; color:#8b949e; border-bottom:1px solid #21262d;
     margin:1.4rem 0 .5rem; padding-bottom:.2rem; }
#status { color:#8b949e; font-size:.8rem; }
table { border-collapse:collapse; width:100%; margin-bottom:.5rem; }
th { color:#8b949e; text-align:left; font-weight:normal; font-size:.8rem;
     border-bottom:1px solid #30363d; padding:.25rem .6rem; }
td { padding:.25rem .6rem; border-bottom:1px solid #161b22; vertical-align:top; }
tr:hover td { background:#161b22; cursor:pointer; }
.state { font-weight:bold; }
.RECEIVED,.PLANNED { color:#8b949e; }
.PLANNING,.DISPATCHED,.CLAIMED,.CODING,.REVIEWING { color:#d29922; }
.CRITIQUE { color:#a371f7; }
.DONE { color:#3fb950; }
.FAILED,.CANCELLED { color:#f85149; }
.ok { color:#3fb950; } .warn { color:#d29922; } .bad { color:#f85149; }
pre { background:#010409; border:1px solid #21262d; border-radius:6px;
      padding:.6rem; overflow-x:auto; font-size:.75rem; }
#detail { margin-top:.6rem; }
button { background:#21262d; color:#c9d1d9; border:1px solid #30363d;
         border-radius:4px; padding:.15rem .5rem; cursor:pointer; font-size:.7rem; }
button:hover { background:#30363d; }
a { color:#58a6ff; text-decoration:none; }
</style>
</head>
<body>
<h1>memnc mesh console</h1>
<div id="status">connecting…</div>
<h2>Nodes</h2><table id="nodes"></table>
<h2>Tasks</h2><table id="tasks"></table>
<h2>Node roster (capabilities)</h2><table id="caps"></table>
<h2>Grants</h2><table id="grants"></table>
<h2>Trust</h2><table id="trust"></table>
<h2>Ledger</h2><pre id="ledger">…</pre>
<div id="detail"></div>
<script>
const esc = s => String(s ?? "").replace(/[&<>"]/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function fill(id, rows) {
  const el = document.getElementById(id);
  if (rows) el.innerHTML = rows;
}
async function poll() {
  const t0 = performance.now();
  try {
    const r = await fetch("/api/state");
    const d = await r.json();
    document.getElementById("status").textContent =
      "broker " + esc(d.nats_url) + " · live · " + d.tasks.length + " tasks, "
      + d.caps.length + " capabilities · " + new Date().toLocaleTimeString();
    fill("nodes",
      "<tr><th>node</th><th>roles</th><th>models</th><th>vram</th>" +
      "<th>paused</th><th>updated</th><th></th></tr>" +
      d.nodes.map(n =>
        `<tr><td>${esc(n.node_id)}</td>` +
        `<td>${esc(n.roles.join(", "))}</td>` +
        `<td>${esc(n.models.join(", "))}</td>` +
        `<td>${esc(n.vram_gb)} GB ${esc(n.gpu)}</td>` +
        `<td class="${n.paused ? "bad" : "ok"}">${n.paused ? "PAUSED" : "live"}</td>` +
        `<td>${esc(n.updated_at)}</td>` +
        `<td><button onclick="control({action:'${n.paused ? "resume_node" : "pause_node"}',node:'${esc(n.node_id)}'},this)">` +
        `${n.paused ? "resume" : "pause"}</button></td></tr>`).join(""));
    fill("tasks",
      "<tr><th>task</th><th>state</th><th>rev</th><th>created</th><th>task text</th><th></th></tr>" +
      d.tasks.map(t =>
        `<tr data-id="${esc(t.task_id)}"><td>${esc(t.task_id)}</td>` +
        `<td class="state ${esc(t.state)}">${esc(t.state)}</td>` +
        `<td>${t.revisions}</td><td>${esc(t.created_at)}</td>` +
        `<td>${esc(t.task)}</td>` +
        `<td>${t.terminal ? "" :
          `<button onclick="control({action:'cancel',task_id:'${esc(t.task_id)}'},this)">cancel</button> ` +
          `<button onclick="control({action:'retry',task_id:'${esc(t.task_id)}'},this)">retry</button>`}</td></tr>`).join(""));
    fill("caps",
      "<tr><th>agent</th><th>skill</th><th>node</th><th>load</th><th>model</th></tr>" +
      d.caps.map(c =>
        `<tr><td>${esc(c.agent_id)}</td><td>${esc(c.skill)}</td>` +
        `<td>${esc(c.node ?? "-")}</td>` +
        `<td class="${c.load/c.capacity > .8 ? "bad" : "ok"}">${c.load}/${c.capacity}</td>` +
        `<td>${esc(c.model ?? "-")}</td></tr>`).join(""));
    fill("grants",
      "<tr><th>agent</th><th>level</th><th>by</th><th>expires</th></tr>" +
      d.grants.map(g =>
        `<tr><td>${esc(g.agent_id)}</td><td class="${g.level === "root" ? "warn" : "ok"}">` +
        `${esc(g.level)}</td><td>${esc(g.granted_by)}</td><td>${esc(g.expires)}</td></tr>`)
      .join(""));
    fill("trust",
      "<tr><th>agent</th><th>score</th><th>last</th></tr>" +
      d.trust.map(t =>
        `<tr><td>${esc(t.subject)}</td>` +
        `<td class="${t.score < .3 ? "bad" : "ok"}">${t.score.toFixed(2)}</td>` +
        `<td>${esc(t.last_outcome ?? "")}</td></tr>`).join(""));
    fill("ledger", esc(JSON.stringify(d.ledger, null, 2)));
  } catch (e) {
    document.getElementById("status").innerHTML =
      '<span class="bad">broker unreachable: ' + esc(e.message) + "</span>";
  }
}
async function control(payload, btn) {
  if (btn) { btn.disabled = true; }
  try {
    const r = await fetch("/api/control", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload),
    });
    const d = await r.json();
    if (d.error) { document.getElementById("status").innerHTML = '<span class="bad">' + esc(d.error) + "</span>"; }
    else { await poll(); }
  } catch (e) {
    document.getElementById("status").innerHTML = '<span class="bad">' + esc(e.message) + "</span>";
  } finally {
    if (btn) { btn.disabled = false; }
  }
}
document.getElementById("tasks").addEventListener("click", async ev => {
  if (ev.target.tagName === "BUTTON") return;  // action buttons, not detail
  const tr = ev.target.closest("tr[data-id]");
  if (!tr) return;
  document.getElementById("detail").textContent = "loading " + tr.dataset.id + "…";
  try {
    const r = await fetch("/api/task/" + tr.dataset.id);
    document.getElementById("detail").innerHTML =
      "<h2>Task " + esc(tr.dataset.id) + "</h2><pre>" +
      esc(JSON.stringify(await r.json(), null, 2)) + "</pre>";
  } catch (e) { document.getElementById("detail").textContent = "error: " + e; }
});
poll(); setInterval(poll, _REFRESH_MS_PLACEHOLDER);
</script>
</body></html>
""".replace("_REFRESH_MS_PLACEHOLDER", str(_REFRESH_MS))


def _format_grants(grants) -> list[dict]:
    now = time.time()
    rows = []
    for g in grants:
        rows.append({
            "agent_id": g.agent_id,
            "level": g.level.value,
            "granted_by": g.granted_by,
            "expires": (
                f"in {g.expires_at - now:.0f}s" if g.expires_at > 0 else "never"
            ),
        })
    return rows


async def collect_state(settings) -> dict:
    bus = Bus(settings)
    await bus.connect(attempts=2)
    try:
        tasks = await bus.list_tasks()
        caps = await bus.list_capabilities()
        grants = await bus.list_grants()
        trust = await bus.list_trust()
        ledger = await bus.read_ledger()
        nodes = await bus.list_nodes()
    finally:
        await bus.close()
    return {
        "nats_url": settings.nats_url,
        "nodes": [
            {
                "node_id": n.node_id,
                "roles": n.roles,
                "models": n.models,
                "vram_gb": n.vram_gb,
                "gpu": n.gpu,
                "paused": n.paused,
                "updated_at": n.updated_at,
            }
            for n in sorted(nodes, key=lambda n: n.node_id)
        ],
        "tasks": [
            {
                "task_id": t.task_id,
                "state": t.state.value,
                "revisions": t.revisions,
                "created_at": t.created_at,
                "task": t.task[:80],
                "terminal": t.state in TERMINAL_STATES,
            }
            for t in sorted(tasks, key=lambda s: s.created_at, reverse=True)
        ],
        "caps": [
            {
                "agent_id": c.agent_id,
                "skill": c.skill,
                "node": c.node,
                "load": c.load,
                "capacity": c.capacity,
                "model": c.model,
            }
            for c in sorted(caps, key=lambda c: (c.agent_id, c.skill))
        ],
        "grants": _format_grants(grants),
        "trust": [
            {
                "subject": t.subject,
                "score": t.credit,
                "last_outcome": t.rating,
            }
            for t in trust
        ],
        "ledger": ledger,
    }


class ConsoleHandler(BaseHTTPRequestHandler):
    settings: "Settings" = None  # type: ignore[assignment]  # injected in serve()

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path in ("/", ""):
                self._send(200, _PAGE.encode(), "text/html; charset=utf-8")
            elif path == "/api/state":
                data = asyncio.run(collect_state(self.settings))
                self._send(200, json.dumps(data).encode(), "application/json")
            elif path.startswith("/api/task/"):
                task_id = path.removeprefix("/api/task/")
                data = asyncio.run(self._task(task_id))
                self._send(200, json.dumps(data).encode(), "application/json")
            else:
                self._send(404, b"not found", "text/plain")
        except Exception as exc:  # render errors in the page, not crash the poll
            body = json.dumps({"error": str(exc)}).encode()
            self._send(503, body, "application/json")

    async def _task(self, task_id: str) -> dict:
        bus = Bus(self.settings)
        await bus.connect(attempts=2)
        try:
            snapshot = await bus.get_snapshot(task_id)
            if snapshot is None:
                return {"error": f"task not found: {task_id}"}
            return snapshot.model_dump(mode="json")
        finally:
            await bus.close()

    def do_POST(self) -> None:
        """T4: forward a human control command as agent 'console' (grant-checked)."""
        path = urlparse(self.path).path
        if path != "/api/control":
            self._send(404, b"not found", "text/plain")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            action = payload.get("action", "")
            if action not in ("pause_node", "resume_node", "cancel", "retry"):
                raise ValueError(f"action not allowed: {action!r}")
            data = asyncio.run(self._control(action, payload))
            self._send(200, json.dumps(data).encode(), "application/json")
        except Exception as exc:  # noqa: BLE001
            body = json.dumps({"error": str(exc)}).encode()
            self._send(503, body, "application/json")

    async def _control(self, action: str, payload: dict) -> dict:
        client = await _connect(self.settings)
        try:
            response = await client.request(
                SUBJECT_CONTROL,
                ControlCommand(
                    action=action,  # type: ignore[arg-type]
                    agent_id="console",
                    node=str(payload.get("node", "")),
                    task_id=str(payload.get("task_id", "")).lstrip("#"),
                ).model_dump_json().encode(),
                timeout=15,
            )
            return json.loads(response.data)
        finally:
            await client.close()


def serve(port: int) -> None:
    settings = get_settings()
    ConsoleHandler.settings = settings
    server = ThreadingHTTPServer(("0.0.0.0", port), ConsoleHandler)
    print(f"mesh console: http://localhost:{port}  (broker {settings.nats_url})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="read-only mesh web console")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    serve(args.port)
