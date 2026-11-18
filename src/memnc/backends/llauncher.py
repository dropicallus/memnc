"""llauncher backend: resolve a model to its llama-server port via llauncher's
run-state locks, then speak OpenAI-compatible ``/v1/chat/completions``.

This backend is an OPTION, not a dependency: the mesh connects to whatever
OpenAI-compatible endpoint is configured; llauncher is just one local manager
that publishes running models as ``<llauncher_home>/run/<port>.lock`` with
``{"model": ..., "port": ...}``. Nothing here starts or stops servers — the
mesh only connects.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

from .openai import OpenAIBackend

DEFAULT_LLAUNCHER_HOME = "~/.llauncher"


def _locks_dir(llauncher_home: str | None) -> Path:
    home = Path(
        os.environ.get("MEMNC_LLAUNCHER_HOME", llauncher_home or DEFAULT_LLAUNCHER_HOME)
    ).expanduser()
    return home / "run"


def _read_locks(llauncher_home: str | None) -> dict[str, str]:
    """{model_name: base_url} for every running llauncher-managed server."""
    out: dict[str, str] = {}
    locks = _locks_dir(llauncher_home)
    if not locks.is_dir():
        return out
    for lock in sorted(locks.glob("*.lock")):
        try:
            data = json.loads(lock.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        model, port = data.get("model"), data.get("port")
        if isinstance(model, str) and model and isinstance(port, int) and port > 0:
            out[model] = f"http://127.0.0.1:{port}/v1"
    return out


class LlauncherBackend:
    """Resolve model -> local llama-server port from llauncher run locks.

    Delegates every call to the OpenAI-compatible backend; llama-servers expose
    ``/v1/chat/completions`` natively, so no new protocol is involved.
    """

    def __init__(
        self,
        base_url: str = "",  # unused; resolved per model (kept for signature parity)
        *,
        llauncher_home: str | None = None,
        api_key: str | None = None,
        timeout: float = 180.0,
        max_retries: int = 3,
        client: httpx.AsyncClient | None = None,
    ):
        self.llauncher_home = llauncher_home
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None
        self._resolved: dict[str, OpenAIBackend] = {}

    def resolve(self, model: str) -> OpenAIBackend:
        if model in self._resolved:
            return self._resolved[model]
        locks = _read_locks(self.llauncher_home)
        base_url = locks.get(model)
        if not base_url:
            running = ", ".join(sorted(locks)) or "none"
            raise RuntimeError(
                f"llauncher: no running server for model {model!r} "
                f"(running: {running})"
            )
        backend = OpenAIBackend(
            base_url,
            api_key=self.api_key,
            timeout=self.timeout,
            max_retries=self.max_retries,
            client=self._client,
        )
        self._resolved[model] = backend
        return backend

    async def chat(self, messages, tools=None, model: str = "", **options):
        return await self.resolve(model).chat(messages, tools=tools, model=model, **options)

    async def list_models(self) -> list[str]:
        return sorted(_read_locks(self.llauncher_home))

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
