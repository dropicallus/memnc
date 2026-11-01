"""Inference backends. The mesh only connects; it never serves models."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from ..config import Settings


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChatResult:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


class ChatBackend(Protocol):
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str = "",
        **options: Any,
    ) -> ChatResult: ...

    async def list_models(self) -> list[str]: ...

    async def close(self) -> None: ...


def make_backend(settings: Settings) -> ChatBackend:
    if settings.inference_provider == "ollama":
        from .ollama import OllamaBackend

        return OllamaBackend(
            settings.inference_base_url,
            timeout=settings.inference_timeout_s,
            max_retries=settings.inference_max_retries,
        )
    from .openai import OpenAIBackend

    return OpenAIBackend(
        settings.inference_base_url,
        api_key=settings.inference_api_key,
        timeout=settings.inference_timeout_s,
        max_retries=settings.inference_max_retries,
    )


def endpoint(base_url: str, path: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/v1") and path.startswith("/v1/"):
        path = path[3:]
    return f"{base}{path}"


async def request_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float,
    retries: int,
) -> dict[str, Any]:
    """HTTP with bounded exponential backoff on throttling/transient errors."""

    import asyncio

    delay = 0.5
    for attempt in range(retries + 1):
        try:
            response = await client.request(
                method, url, json=body, headers=headers, timeout=timeout
            )
        except httpx.TransportError:
            if attempt >= retries:
                raise
            await asyncio.sleep(delay)
            delay = min(delay * 2, 8)
            continue

        if response.status_code in (429, 500, 502, 503, 504) and attempt < retries:
            await asyncio.sleep(delay)
            delay = min(delay * 2, 8)
            continue
        response.raise_for_status()
        return response.json()

    raise RuntimeError("request_json exhausted retries")


def parse_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value or "{}")
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}
