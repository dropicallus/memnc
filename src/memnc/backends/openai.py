"""OpenAI-compatible ``/v1/chat/completions`` backend."""

from __future__ import annotations

import json
from typing import Any

import httpx

from . import ChatResult, ToolCall, endpoint, parse_arguments, request_json


class OpenAIBackend:
    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        timeout: float = 180.0,
        max_retries: int = 3,
        client: httpx.AsyncClient | None = None,
    ):
        self.base_url = base_url
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

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
            "messages": messages,
            "stream": False,
            "temperature": options.get("temperature", 0.2),
            "max_tokens": options.get("num_predict", 4096),
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = options.get("tool_choice", "auto")

        data = await request_json(
            self._client,
            "POST",
            endpoint(self.base_url, "/v1/chat/completions"),
            body=body,
            headers=self._headers(),
            timeout=self.timeout,
            retries=self.max_retries,
        )
        choices = data.get("choices") or [{}]
        message = choices[0].get("message") or {}
        calls: list[ToolCall] = []
        for raw in message.get("tool_calls") or []:
            function = raw.get("function") or {}
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments or "{}")
                except json.JSONDecodeError:
                    arguments = {}
            calls.append(
                ToolCall(
                    id=raw.get("id") or f"call_{len(calls)}",
                    name=function.get("name", ""),
                    arguments=parse_arguments(arguments),
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
            endpoint(self.base_url, "/v1/models"),
            headers=self._headers(),
            timeout=min(self.timeout, 15),
            retries=1,
        )
        # OpenAI shape: {"data": [{"id": ...}]}. Some compatible servers
        # (llama.cpp builds, gateways) return an Ollama-like {"models": [...]}.
        entries = data.get("data") or data.get("models") or []
        models: list[str] = []
        for entry in entries:
            if isinstance(entry, str):
                models.append(entry)
                continue
            name = entry.get("id") or entry.get("name") or entry.get("model")
            if name:
                models.append(name)
        return models
