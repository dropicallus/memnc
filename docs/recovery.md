# memnc — Lost source code: blueprint from .pyc remnants

**Note:** The MCP-/backend-/test source in `src/memnc/{agent,bus,cli,config,fsutil,orchestrator,roles,worker}.py`,
`src/memnc/backends/{ollama,openai}.py` and `src/memnc/mcp/{server,client,tools_fs,tools_shell}.py`
was never committed and the `.py` source files were lost. What was recovered through extraction
from `__pycache__/*.pyc` (Python 3.14, marshal): docstrings + function/class signatures.
This serves as a specification for the rebuild. The .pyc files themselves live in
`~/memnc-recovery/` (backup, not in git).

## Modules (reconstructed)
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

## Tests (lost, planned)
`test_agent, test_backends, test_fsm, test_fsutil, test_integration, test_mcp_stdio,
test_roles, test_schema, test_tools, test_worker` (pytest 9.2.0).

## Architecture goal (from the plan, docs/memnc-mesh-plan.md)
Mesh of orchestrator + N workers over NATS JetStream; MCP (stdio) for fs/shell tools;
backends (Ollama/OpenAI) for LLM inference; roles (Planner/Coder/Critic); durable
state via JetStream; no model is shipped — it only connects to inference endpoints.
