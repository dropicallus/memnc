"""Provider-agnostic tool-calling loop and structured output parsing."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from .backends import ChatResult, ToolCall
from .schema import ROLE_OUTPUT, Role


class AgentLimitError(RuntimeError):
    """Raised when the model exceeds the tool-step budget."""


@dataclass
class AgentRun:
    content: str
    tool_steps: int = 0
    messages: list[dict[str, Any]] = field(default_factory=list)


def assistant_message(result: ChatResult) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": result.content or "",
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments),
                },
            }
            for call in result.tool_calls
        ],
    }


def tool_message(call: ToolCall, output: str) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": call.id,
        "name": call.name,
        "content": output,
    }


async def run_agent(
    *,
    backend,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    max_steps: int,
    execute_tool: Callable[[str, dict[str, Any]], Awaitable[str]],
    on_tool_result: Callable[[ToolCall, str], Awaitable[None]] | None = None,
    **chat_options: Any,
) -> AgentRun:
    """Run chat -> tool calls -> chat until the model emits a final answer."""

    steps = 0
    while True:
        result = await backend.chat(messages, tools=tools or None, model=model, **chat_options)
        if not result.tool_calls:
            return AgentRun(content=result.content, tool_steps=steps, messages=messages)

        if steps >= max_steps:
            raise AgentLimitError(f"tool step limit ({max_steps}) exceeded")

        steps += 1
        messages.append(assistant_message(result))
        for call in result.tool_calls:
            try:
                output = await execute_tool(call.name, call.arguments)
            except Exception as exc:  # noqa: BLE001 - surface tool errors to the model
                output = f"tool error: {exc}"
            if on_tool_result is not None:
                await on_tool_result(call, output)
            messages.append(tool_message(call, output))


def extract_json_candidates(text: str) -> list[Any]:
    """Collect every JSON object/array in possibly chatty model output.

    Fenced code blocks first (models often wrap JSON in ```json fences),
    then a scan of the raw text. Order is document order.
    """

    decoder = json.JSONDecoder()
    candidates: list[Any] = []
    for block in re.findall(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL):
        try:
            value, _ = decoder.raw_decode(block.strip())
        except json.JSONDecodeError:
            continue
        if isinstance(value, (dict, list)):
            candidates.append(value)
    scan = 0
    while scan < len(text):
        if text[scan] not in "{[":
            scan += 1
            continue
        try:
            value, end = decoder.raw_decode(text, scan)
        except json.JSONDecodeError:
            scan += 1
            continue
        if isinstance(value, (dict, list)):
            candidates.append(value)
        scan = max(end, scan + 1)
    return candidates


def extract_json(text: str) -> Any:
    """Pull the first JSON object/array out of possibly chatty model output."""

    candidates = extract_json_candidates(text)
    if not candidates:
        raise ValueError("no JSON object found in model output")
    return candidates[0]


def _tool_call_shaped(payload: Any) -> bool:
    """True for JSON tool-call echoes (name/arguments) emitted as text."""
    return isinstance(payload, dict) and {"name", "arguments"} <= set(payload)


def _role_score(model: BaseModel) -> int:
    """Content richness: prefer parsed outputs that actually carry data."""
    score = 0
    for name in ("files", "commands", "issues", "plan", "steps"):
        value = getattr(model, name, None)
        if value:
            score += len(value)
    if getattr(model, "summary", ""):
        score += 1
    if getattr(model, "question", ""):
        score += 1
    if getattr(model, "answer", ""):
        score += 1
    return score


def parse_role_output(role: Role, text: str) -> BaseModel:
    """Parse a role payload, tolerating chatter and tool-call echoes.

    Models sometimes emit a JSON tool call as plain text before their real
    result object. All JSON candidates are validated and the richest valid
    payload wins (document order breaks ties).
    """

    candidates = [c for c in extract_json_candidates(text) if not _tool_call_shaped(c)]
    best: BaseModel | None = None
    best_score = -1
    first_error: ValidationError | None = None
    for payload in candidates:
        if not isinstance(payload, dict):
            continue
        try:
            model = ROLE_OUTPUT[role].model_validate(payload)
        except ValidationError as exc:
            if first_error is None:
                first_error = exc
            continue
        score = _role_score(model)
        if score > best_score:
            best, best_score = model, score
    if best is not None:
        return best
    if first_error is not None:
        raise first_error
    raise ValueError("model output must be a JSON object")
