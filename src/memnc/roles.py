"""Role prompts and context rendering."""

from __future__ import annotations

import json
from typing import Any

from . import journal
from .schema import MessageTuple, Role

# Recorded with every bench result and worker deposit. Bump it when the prompt
# text changes; records written under an older value keep that value.
PROMPT_VERSION = "1.1"

MAX_FILE_CHARS = 20_000
MAX_PROMPT_CHARS = 90_000

SYSTEM_PROMPTS: dict[Role, str] = {
    Role.PLANNER: (
        "You are the PLANNER in a multi-agent software engineering mesh. "
        "Decompose the task into a short, ordered, actionable plan. "
        "Name the files that must be created or modified. Do not write code. "
        "If the task is ambiguous or missing information you cannot resolve, "
        'do NOT guess: reply with {"question": "your question"} instead of a plan; '
        "the task is paused until a human answers and you are re-invoked with the answer "
        "appended to the task text. "
        'Optionally add an "acceptance" field per plan step with machine-checkable '
        "criteria, one per line. Supported kinds: `file_exists <path>`, "
        "`file_min_size <path> <min_bytes>`, `file_contains <path> <regex>`, "
        "`command <argv>` (must exit 0), `ffprobe_duration_min <path> <min_seconds>`. "
        "Paths in acceptance criteria are workspace-relative (e.g. `report.md`), "
        "never absolute. "
        "Use them only when the deliverable is mechanically checkable; prose-only "
        "acceptance text is ignored by the checker. "
        "Reply with ONLY a JSON object of the form:\n"
        '{"plan": [{"step": 1, "description": "...", "files": ["path"]}], "notes": "..."}\n'
        'or {"question": "..."} to ask back. No prose, no markdown fences.'
    ),
    Role.CODER: (
        "You are the CODER in a multi-agent software engineering mesh. "
        "Implement the task by returning the COMPLETE content of every file that "
        "must be created or changed. You may use the provided tools to inspect the "
        "workspace and run commands, but your final message MUST be only a JSON object:\n"
        '{"summary": "...", "files": [{"path": "relative/path", "content": "full file"}], '
        '"commands": ["optional shell command"]}\n'
        "Every file content must be complete and runnable; never use placeholders or diffs. "
        "If you write files with tools, you MUST still return their full contents in the "
        "JSON response. Before finishing, verify every Python file compiles "
        "(for example run `python -m py_compile <file>` with run_shell when available). "
        "If you are revising, address every issue from the critique. "
        "If a blocking requirement is ambiguous or impossible without a decision, "
        'reply with {"question": "your question"} instead of files; the task pauses '
        "until a human answers. "
        "No prose, no markdown fences."
    ),
    Role.CRITIC: (
        "You are the CRITIC in a multi-agent software engineering mesh. "
        "Review the implementation against the task and the plan. Do not rewrite code. "
        "When tools are available, verify the files you were given (for example run "
        "`python -m py_compile <file>`), and do not approve code that fails to parse. "
        "Reply with ONLY a JSON object of the form:\n"
        '{"approved": true|false, "summary": "...", '
        '"issues": [{"file": "...", "line": 1, "severity": "minor|major|blocker", '
        '"issue": "...", "fix": "..."}]}\n'
        "If you need information from the human (for example which of two valid "
        'interpretations applies), reply with {"question": "your question"} instead of a '
        "verdict; the task pauses until a human answers. "
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


def build_user_prompt(
    role: Role,
    payload: dict[str, Any],
    message_tuple: MessageTuple | None = None,
) -> str:
    """Render task context for the model from the orchestrator payload.

    With a message tuple (docs/roadmap.md) the task/context/plan
    sections are replaced by the structured MESSAGE / BACKGROUND /
    INTERMEDIATE OUTPUT sections; all role-specific sections below are kept.
    """

    if message_tuple is not None:
        bg = message_tuple.background
        bg_parts = [f"TASK:\n{bg.task}"]
        if bg.context:
            bg_parts.append(f"CONTEXT:\n{bg.context}")
        if bg.plan is not None:
            bg_parts.append(f"PLAN:\n{_dump(bg.plan.model_dump())}")
        for assumption in bg.assumptions:
            bg_parts.append(f"ASSUMPTION: {assumption}")
        parts = [
            f"MESSAGE:\n{message_tuple.message}",
            "BACKGROUND (shared, read-only):\n" + "\n".join(bg_parts),
        ]
        io = message_tuple.intermediate_output
        if io is not None:
            io_parts = [f"ROLE: {io.role.value}"]
            if io.summary:
                io_parts.append(f"SUMMARY:\n{io.summary}")
            if io.files:
                io_parts.append(f"FILES:\n{_format_files([f.model_dump() for f in io.files])}")
            if io.issues:
                io_parts.append(f"ISSUES:\n{_dump([i.model_dump() for i in io.issues])}")
            parts.append("INTERMEDIATE OUTPUT (superseded by your result):\n" + "\n".join(io_parts))
    else:
        parts: list[str] = [f"TASK:\n{payload.get('task', '')}"]
        context = payload.get("context")
        if context:
            parts.append(f"ADDITIONAL CONTEXT:\n{context}")

        plan = payload.get("plan")
        if plan:
            parts.append(f"PLAN:\n{_dump(plan)}")

    workspace_state = payload.get("workspace_state")
    if workspace_state:
        parts.append(
            "WORKSPACE STATE (prior attempts left files in the shared "
            "workspace; inspect it before redoing or planning work):\n"
            f"{workspace_state}"
        )

    # 15.4.3 attempt journal: compact checkpoint notes from failed attempts.
    journal_block = journal.render(payload.get("attempt_journal"))
    if journal_block:
        parts.append(journal_block)

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

    last_error = payload.get("last_error")
    if last_error and role is Role.CODER:
        parts.append(
            "YOUR PREVIOUS ATTEMPT FAILED:\n"
            f"{last_error}\n"
            "Fix this before answering, and verify it with the tools "
            "(for example `python -m py_compile <file>`)."
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
            "Verify claims against the actual files before judging: read the "
            "changed files with read_file and, when the task is runnable code, "
            "execute it with run_shell (e.g. `python <file>`). Only issue a "
            "REVISE verdict for defects you observed yourself in tool output "
            "or file contents — never from assumption. If verification shows "
            "the task is fulfilled, return state DONE. Return the JSON verdict only."
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


# M16 mesh-clarify: prompt for peer consults when a parked agent asks.
CONSULT_SYSTEM_PROMPT = (
    "You are a peer agent in a multi-agent software engineering mesh. "
    "Another agent in the pipeline was blocked by a question about the task "
    "and asks you to resolve it. Answer from the task context, your own "
    "output, and general software engineering knowledge — do not guess at "
    "facts you cannot know. Reply with ONLY a JSON object of the form:\n"
    '{"answer": "your best resolution of the question"}\n'
    "or, if the question needs the human's decision (a preference, a "
    "credential, information nobody in the mesh has), reply with\n"
    '{"cannot_answer": "why"}. No prose, no markdown fences.'
)


def build_consult_user_prompt(role: Role, payload: dict[str, Any]) -> str:
    """Render the consult command payload for one peer role."""
    consult = payload.get("consult") or {}
    parts = [
        f"Question from the task's {consult.get('from_role', 'agent')}: "
        + str(consult.get("question", "")),
        "Task:\n" + str(payload.get("task", "")),
    ]
    if payload.get("context"):
        parts.append("Context:\n" + str(payload["context"]))
    own = {
        Role.PLANNER: payload.get("plan"),
        Role.CODER: payload.get("code"),
        Role.CRITIC: payload.get("critique"),
    }.get(role)
    if own is not None:
        parts.append(f"Your {role.value} output on this task:\n" + json.dumps(own))
    parts.append("Reply with ONLY the JSON object described in your instructions.")
    return "\n\n".join(parts)
