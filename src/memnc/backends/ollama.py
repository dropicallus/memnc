"""Ollama native ``/api/chat`` backend."""

from __future__ import annotations

import json
from typing import Any

import httpx

from . import ChatResult, ToolCall, endpoint, parse_arguments, request_json


class OllamaBackend:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 180.0,
        max_retries: int = 3,
        client: httpx.AsyncClient | None = None,
    ):
        self.base_url = base_url
        self.timeout = timeout
        self.max_retries = max_retries
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str = "",
        **options: Any,
    ) -> ChatResult:
        body: dict[str, Any] = {
            "model": model,
            "messages": _to_ollama_messages(messages),
            "stream": False,
            "keep_alive": options.get("keep_alive", "5m"),
            "options": {
                "temperature": options.get("temperature", 0.2),
                "num_ctx": options.get("num_ctx", 8192),
                "num_predict": options.get("num_predict", 4096),
            },
        }
        if tools:
            body["tools"] = tools

        data = await request_json(
            self._client,
            "POST",
            endpoint(self.base_url, "/api/chat"),
            body=body,
            timeout=self.timeout,
            retries=self.max_retries,
        )
        message = data.get("message") or {}
        calls: list[ToolCall] = []
        for index, raw in enumerate(message.get("tool_calls") or []):
            function = raw.get("function") or {}
            calls.append(
                ToolCall(
                    id=raw.get("id") or f"call_{index}",
                    name=function.get("name", ""),
                    arguments=parse_arguments(function.get("arguments")),
                )
            )
        return ChatResult(
            content=message.get("content") or "",
            tool_calls=calls,
            raw=data,
        )

    async def list_models(self) -> list[str]:
        data = await request_json(
            self._client,
            "GET",
            endpoint(self.base_url, "/api/tags"),
            timeout=min(self.timeout, 15),
            retries=1,
        )
        return [
            model.get("name") or model.get("model") or ""
            for model in data.get("models", [])
        ]


def _to_ollama_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate internal OpenAI-shaped messages into Ollama's shape."""

    converted: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "assistant" and message.get("tool_calls"):
            calls = []
            for call in message["tool_calls"]:
                function = call.get("function") or {}
                arguments = function.get("arguments")
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments or "{}")
                    except json.JSONDecodeError:
                        arguments = {}
                calls.append(
                    {"function": {"name": function.get("name"), "arguments": arguments}}
                )
            converted.append(
                {
                    "role": "assistant",
                    "content": message.get("content") or "",
                    "tool_calls": calls,
                }
            )
        elif role == "tool":
            entry: dict[str, Any] = {
                "role": "tool",
                "content": message.get("content") or "",
            }
            if message.get("name"):
                entry["tool_name"] = message["name"]
            converted.append(entry)
        else:
            converted.append({"role": role, "content": message.get("content") or ""})
    return converted
