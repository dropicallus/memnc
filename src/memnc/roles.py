"""Role prompts and context rendering."""

from __future__ import annotations

import json
from typing import Any

from .schema import Role

PROMPT_VERSION = "2026-10-31.1"

MAX_FILE_CHARS = 20_000
MAX_PROMPT_CHARS = 90_000

SYSTEM_PROMPTS: dict[Role, str] = {
    Role.PLANNER: (
        "You are the PLANNER in a multi-agent software engineering mesh. "
        "Decompose the task into a short, ordered, actionable plan. "
        "Name the files that must be created or modified. Do not write code. "
        "Reply with ONLY a JSON object of the form:\n"
        '{"plan": [{"step": 1, "description": "...", "files": ["path"]}], "notes": "..."}\n'
        "No prose, no markdown fences."
    ),
    Role.CODER: (
        "You are the CODER in a multi-agent software engineering mesh. "
        "Implement the task by returning the COMPLETE content of every file that "
        "must be created or changed. You may use the provided tools to inspect the "
        "workspace and run commands, but your final message MUST be only a JSON object:\n"
        '{"summary": "...", "files": [{"path": "relative/path", "content": "full file"}], '
        '"commands": ["optional shell command"]}\n'
        "Every file content must be complete and runnable; never use placeholders or diffs. "
        "If you are revising, address every issue from the critique. No prose, no markdown fences."
    ),
    Role.CRITIC: (
        "You are the CRITIC in a multi-agent software engineering mesh. "
        "Review the implementation against the task and the plan. Do not rewrite code. "
        "Reply with ONLY a JSON object of the form:\n"
        '{"approved": true|false, "summary": "...", '
        '"issues": [{"file": "...", "line": 1, "severity": "minor|major|blocker", '
        '"issue": "...", "fix": "..."}]}\n'
        "Approve only if the implementation is correct, complete and safe. No prose, no markdown fences."
    ),
}


def _dump(value: Any) -> str:
    return json.dumps(value, indent=2, default=str)


def _format_files(files: list[dict[str, Any]]) -> str:
    chunks: list[str] = []
    total = 0
    for item in files:
        content = item.get("content", "")
        if len(content) > MAX_FILE_CHARS:
            content = content[:MAX_FILE_CHARS] + "\n...[truncated]"
        chunk = f"--- {item.get('path', '?')} ---\n{content}"
        if total + len(chunk) > MAX_PROMPT_CHARS:
            chunks.append("...[more files omitted]")
            break
        chunks.append(chunk)
        total += len(chunk)
    return "\n\n".join(chunks) or "(no files yet)"


def build_user_prompt(role: Role, payload: dict[str, Any]) -> str:
    """Render task context for the model from the orchestrator payload."""

    parts: list[str] = [f"TASK:\n{payload.get('task', '')}"]
    context = payload.get("context")
    if context:
        parts.append(f"ADDITIONAL CONTEXT:\n{context}")

    plan = payload.get("plan")
    if plan:
        parts.append(f"PLAN:\n{_dump(plan)}")

    code = payload.get("code")
    if code:
        parts.append(f"CURRENT IMPLEMENTATION SUMMARY:\n{code.get('summary', '')}")
        parts.append(f"CURRENT FILES:\n{_format_files(code.get('files', []))}")

    critique = payload.get("critique")
    if critique and role is Role.CODER:
        parts.append(
            "CRITIQUE TO ADDRESS (this is revision "
            f"{payload.get('revisions', 0)}):\n{_dump(critique)}"
        )

    workspace = payload.get("workspace_root")
    if workspace:
        parts.append(
            f"WORKSPACE: {workspace}. Tools: read_file, write_file, list_dir, "
            "search_text, run_shell."
        )

    if role is Role.CRITIC:
        parts.append(
            "Decide whether the implementation satisfies the task and plan. "
            "Return the JSON verdict only."
        )
    elif role is Role.CODER:
        parts.append(
            "Return the complete final files as JSON. If you used tools to write files, "
            "still include their full contents in the JSON response."
        )
    else:
        parts.append("Return the plan as JSON only.")

    prompt = "\n\n".join(parts)
    if len(prompt) > MAX_PROMPT_CHARS * 2:
        prompt = prompt[: MAX_PROMPT_CHARS * 2] + "\n...[prompt truncated]"
    return prompt
