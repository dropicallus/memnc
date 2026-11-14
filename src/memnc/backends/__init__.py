"""Inference backends. The mesh only connects; it never serves models."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from ..config import Settings

log = logging.getLogger(__name__)


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


def _single_backend(
    provider: str,
    base_url: str,
    *,
    api_key: str | None = None,
    timeout: float = 180.0,
    max_retries: int = 3,
) -> ChatBackend:
    if provider == "ollama":
        from .ollama import OllamaBackend

        return OllamaBackend(base_url, timeout=timeout, max_retries=max_retries)
    from .openai import OpenAIBackend

    return OpenAIBackend(
        base_url,
        api_key=api_key,
        timeout=timeout,
        max_retries=max_retries,
    )


def parse_model_map(raw: str) -> dict[str, str]:
    """Parse ``"primary=fallback,primary2=fallback2"`` into a dict."""

    mapping: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        primary, fallback = pair.split("=", 1)
        primary, fallback = primary.strip(), fallback.strip()
        if primary and fallback:
            mapping[primary] = fallback
    return mapping


def make_backend(settings: Settings) -> ChatBackend:
    primary = _single_backend(
        settings.inference_provider,
        settings.inference_base_url,
        api_key=settings.inference_api_key,
        timeout=settings.inference_timeout_s,
        max_retries=settings.inference_max_retries,
    )
    if not settings.inference_fallback_provider:
        return primary
    fallback = _single_backend(
        settings.inference_fallback_provider,
        settings.inference_fallback_base_url,
        api_key=settings.inference_fallback_api_key,
        timeout=settings.inference_timeout_s,
        max_retries=settings.inference_max_retries,
    )
    return FallbackBackend(
        primary,
        fallback,
        model_map=parse_model_map(settings.inference_fallback_model_map),
    )


class FallbackBackend:
    """Try the primary backend; on connectivity failure degrade to the fallback.

    The switch is sticky: once degraded, the fallback serves traffic and the
    primary is re-probed every ``probe_every`` chats to recover automatically.
    Model names are remapped through ``model_map`` so nodes can name peers'
    models even when the primary model ids differ.
    """

    def __init__(
        self,
        primary: ChatBackend,
        fallback: ChatBackend,
        *,
        model_map: dict[str, str] | None = None,
        probe_every: int = 25,
    ):
        self.primary = primary
        self.fallback = fallback
        self.model_map = model_map or {}
        self.probe_every = max(1, probe_every)
        self._degraded = False
        self._calls = 0

    async def close(self) -> None:
        for backend in (self.primary, self.fallback):
            try:
                await backend.close()
            except Exception:  # noqa: BLE001 - close is best effort
                pass

    async def list_models(self) -> list[str]:
        try:
            return await self.primary.list_models()
        except Exception:  # noqa: BLE001 - list is diagnostic only
            return await self.fallback.list_models()

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str = "",
        **options: Any,
    ) -> ChatResult:
        self._calls += 1
        mapped = self.model_map.get(model, model)
        if not self._degraded:
            try:
                return await self.primary.chat(messages, tools=tools, model=model, **options)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                log.warning(
                    "primary inference unreachable, degrading to fallback: %s",
                    exc,
                )
                self._degraded = True
        else:
            # Periodically re-probe the primary so we recover automatically.
            if self._calls % self.probe_every == 0:
                try:
                    result = await self.primary.chat(messages, tools=tools, model=model, **options)
                    log.info("primary inference recovered, leaving fallback")
                    self._degraded = False
                    return result
                except (httpx.TransportError, httpx.TimeoutException):
                    log.debug("primary still unreachable, staying on fallback")
        return await self.fallback.chat(messages, tools=tools, model=mapped, **options)


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
