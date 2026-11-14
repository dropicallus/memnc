"""Harness abstraction: swappable agent executors above the inference backends.

The worker's in-proc tool loop (``memnc.agent.run_agent``) is one harness;
external coding agents (pi today, OpenCode/Claude Code later) are others.
The contract is deliberately narrow: a harness receives a task prompt and a
workspace directory and returns the resulting workspace state (git diff) plus
a final summary -- never structured patches. Consumers (worker, critic,
routing policy) stay harness-agnostic, which is what makes weak->strong
handoffs and harness swap-out pure configuration.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass
class HarnessResult:
    """Outcome of one harness run. ``diff`` is the product, not patches."""

    ok: bool
    summary: str = ""
    diff: str = ""
    tool_events: int = 0
    exit_code: int | None = None
    error: str | None = None
    transcript_path: str | None = None


@dataclass
class HarnessCaps:
    """What a harness can do; routing may key off these."""

    native_tools: bool = True
    external_process: bool = False
    structured_output: bool = False


@runtime_checkable
class Harness(Protocol):
    name: str

    def capabilities(self) -> HarnessCaps: ...

    async def run(
        self, prompt: str, workspace: Path, *, timeout_s: int = 600
    ) -> HarnessResult: ...


_REGISTRY: dict[str, type] = {}


def register(cls: type) -> type:
    name = getattr(cls, "name", None)
    if not isinstance(name, str) or not name:
        raise ValueError("harness classes need a string 'name' attribute")
    _REGISTRY[name] = cls
    return cls


def get_harness(name: str, **kwargs) -> Harness:
    """Instantiate a registered harness by name."""
    if not _REGISTRY:  # lazy: import plugins on first use
        from . import inproc, pi  # noqa: F401  (register themselves)

    try:
        cls = _REGISTRY[name]
    except KeyError:
        raise KeyError(f"unknown harness {name!r}; known: {known_harnesses()}") from None
    return cls(**kwargs)


def known_harnesses() -> list[str]:
    if not _REGISTRY:
        from . import inproc, pi  # noqa: F401

    return sorted(_REGISTRY)


__all__ = [
    "Harness",
    "HarnessCaps",
    "HarnessResult",
    "get_harness",
    "known_harnesses",
    "register",
]
