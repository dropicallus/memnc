"""Provider-agnostic tool-calling loop and structured output parsing."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

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


def extract_json(text: str) -> Any:
    """Pull the first JSON object/array out of possibly chatty model output.

    Tries fenced code blocks first (models often wrap JSON in ```json fences),
    then scans the raw text for the first parseable object or array.
    """

    decoder = json.JSONDecoder()
    for block in re.findall(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL):
        try:
            value, _ = decoder.raw_decode(block.strip())
        except json.JSONDecodeError:
            continue
        if isinstance(value, (dict, list)):
            return value
    for index, char in enumerate(text):
        if char not in "{[":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, (dict, list)):
            return value
    raise ValueError("no JSON object found in model output")


def parse_role_output(role: Role, text: str) -> BaseModel:
    payload = extract_json(text)
    if not isinstance(payload, dict):
        raise ValueError("model output must be a JSON object")
    return ROLE_OUTPUT[role].model_validate(payload)
