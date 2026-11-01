# memnc — Verlorener Quellcode: Blaupause aus .pyc-Resten

**Stand:** Der MCP-/Backend-/Test-Quellcode in `src/memnc/{agent,bus,cli,config,fsutil,orchestrator,roles,worker}.py`,
`src/memnc/backends/{ollama,openai}.py` und `src/memnc/mcp/{server,client,tools_fs,tools_shell}.py`
wurde NIE commited und die `.py`-Quelldateien sind verloren. Gerettet wurden durch Extraktion aus
`__pycache__/*.pyc` (Python 3.14, marshal): Docstrings + Funktions-/Klassensignaturen.
Dies dient als Spezifikation fuer den Neuaufbau. Die .pyc selbst liegen in
`/home/frank/memnc-recovery/` (Backup, nicht im git).

## Module (rekonstruiert)
- `config.py` — `Settings`, `get_settings()` — "Environment-driven configuration shared by orchestrator, workers and MCP."
- `fsutil.py` — `PathEscapeError`, `safe_path()`, `read_text_limited()`, `atomic_write()`, `apply_artifacts()`, `list_tree()` — path-confined fs helpers.
- `agent.py` — `run_agent()`, `extract_json()`, `parse_role_output()`, `AgentLimitError`, `AgentRun`, `assistant_message()`, `tool_message()` — provider-agnostic tool-calling loop + structured output.
- `bus.py` — `Bus` — NATS transport: JetStream stream, KV snapshots, pulls, dedupe.
- `orchestrator.py` — `Orchestrator`, `main()` — "durable state machine over NATS JetStream."
- `roles.py` — `build_user_prompt()`, `_format_files()`, `_dump()` — role prompts + context rendering.
- `worker.py` — `Worker`, `main()`, `tool_key()`, `python_syntax_errors()`, `merge_written_files()` — pull command, run agent loop, publish result.
- `mcp/server.py` — `create_server()`, `main()` — "MCP stdio server exposing local filesystem and shell tools."
- `mcp/client.py` — `MCPClient`, `_to_openai_tool()` — "MCP stdio client used by workers."
- `mcp/tools_fs.py` — `read_file()`, `write_file()`, `list_dir()`, `search_text()` — plain functions, unit-testable.
- `mcp/tools_shell.py` — `run_shell()` — "Full shell by policy; container is the sandbox."
- `backends/ollama.py` — `OllamaBackend`, `_to_ollama_messages()` — native `/api/chat`.
- `backends/openai.py` — `OpenAIBackend` — `/v1/chat/completions`.

## Tests (verloren, geplant gewesen)
`test_agent, test_backends, test_fsm, test_fsutil, test_integration, test_mcp_stdio,
test_roles, test_schema, test_tools, test_worker` (pytest 9.2.0).

## Architekturziel (aus dem Plan, docs/memnc-mesh-plan.md)
Mesh aus Orchestrator + N Workers ueber NATS JetStream; MCP (stdio) fuer fs/shell-Tools;
Backends (Ollama/OpenAI) fuer LLM-Inference; Rollen (Planner/Coder/Critic); durabler
Zustand ueber JetStream; es wird KEIN Modell geshippt — verbindet sich nur mit Inference-Endpoints.
