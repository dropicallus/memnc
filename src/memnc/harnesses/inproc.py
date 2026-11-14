"""In-proc tool-loop harness: the built-in coder loop as a Harness.

Wraps ``memnc.agent.run_agent`` so the same workspace-state contract
(``HarnessResult``) covers both the built-in loop and external coding
agents. This makes in-proc vs pi vs future harnesses a pure routing
decision (14.14) and lets the probe CLI drive any backend.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..agent import parse_role_output, run_agent
from ..fsutil import apply_artifacts
from ..roles import SYSTEM_PROMPTS, build_user_prompt
from ..schema import Role
from . import HarnessCaps, HarnessResult, register
from .workspace import workspace_diff


@register
class ToolLoopHarness:
    """Run the in-proc tool loop and harvest its artifacts into the workspace."""

    name = "inproc"

    def __init__(
        self,
        model: str,
        backend: Any,
        tools: list[dict[str, Any]] | None = None,
        execute_tool: Callable[[str, dict], Any] | None = None,  # async callable
        system_prompt: str | None = None,
        max_steps: int = 8,
        max_artifact_bytes: int = 200_000,
        raw_prompt: bool = False,
        **chat_options: Any,
    ):
        self.model = model
        self.backend = backend
        self.tools = list(tools or [])
        self.execute_tool = execute_tool
        self.system_prompt = system_prompt
        self.max_steps = max_steps
        self.max_artifact_bytes = max_artifact_bytes
        self.raw_prompt = raw_prompt
        self.chat_options = chat_options

    def capabilities(self) -> HarnessCaps:
        return HarnessCaps(native_tools=True, external_process=False, structured_output=True)

    async def run(self, prompt: str, workspace: Path, *, timeout_s: int = 600) -> HarnessResult:
        workspace = Path(workspace)
        workspace.mkdir(parents=True, exist_ok=True)
        # Same prompt shape as the worker's in-proc coder path: the harness
        # receives the raw task and asks the model for CoderOutput JSON.
        user_prompt = (
            prompt if self.raw_prompt else build_user_prompt(Role.CODER, {"task": prompt})
        )
        messages = [{"role": "user", "content": user_prompt}]
        if self.system_prompt:
            messages.insert(0, {"role": "system", "content": self.system_prompt})
        else:
            messages.insert(0, {"role": "system", "content": SYSTEM_PROMPTS[Role.CODER]})
        try:
            run = await asyncio.wait_for(
                run_agent(
                    backend=self.backend,
                    model=self.model,
                    messages=messages,
                    tools=self.tools,
                    max_steps=self.max_steps,
                    execute_tool=self.execute_tool or _no_tools,
                    **self.chat_options,
                ),
                timeout=timeout_s,
            )
        except TimeoutError:
            return HarnessResult(ok=False, error=f"in-proc loop timed out after {timeout_s}s")
        except Exception as exc:  # noqa: BLE001
            return HarnessResult(ok=False, error=f"in-proc loop error: {exc}")

        try:
            output = parse_role_output(Role.CODER, run.content)
        except (ValueError, ValidationError) as exc:
            return HarnessResult(
                ok=False,
                error=f"invalid coder output: {exc}",
                tool_events=run.tool_steps,
            )

        apply_artifacts(workspace, output.files, self.max_artifact_bytes)
        diff = await workspace_diff(workspace)
        return HarnessResult(
            ok=True,
            summary=output.summary,
            diff=diff,
            tool_events=run.tool_steps,
        )


async def _no_tools(name: str, arguments: dict) -> str:
    """Default executor when the harness runs without MCP tools."""
    return f"tool {name!r} not available in harness mode"
