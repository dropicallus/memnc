"""DiscussHarness: multi-model debate as a coder harness (14.14 item 4).

The debate object is the PLAN, not the code: every discuss model proposes a
solution to the task as text, one critique round sharpens the comparison, a
judge picks a winner, and the winner is executed by the in-proc tool loop so
the harness contract (workspace diff + summary) stays unchanged.

Design note: docs/roadmap.md. Cost caveat: 2K+1 extra inference
calls vs one for inproc -- this is for hard tasks, not a default.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..roles import SYSTEM_PROMPTS, build_user_prompt
from ..schema import Role
from . import HarnessCaps, HarnessResult, register
from .inproc import ToolLoopHarness

_WINNER_RE = re.compile(r"winner\s*[:=]\s*([A-Za-z0-9._/\-]+)", re.IGNORECASE)


@register
class DiscussHarness:
    """Debate the plan among several models, execute the winner's plan."""

    name = "discuss"

    def __init__(
        self,
        model: str,
        backend: Any,
        discuss_models: list[str] | None = None,
        judge_model: str = "",
        tools: list[dict[str, Any]] | None = None,
        execute_tool: Callable[[str, dict], Any] | None = None,
        max_proposal_bytes: int = 20_000,
        **executor_options: Any,
    ):
        self.backend = backend
        self.models = [m for m in (discuss_models or []) if m] or [model]
        self.judge_model = judge_model or self.models[0]
        self.tools = list(tools or [])
        self.execute_tool = execute_tool
        self.max_proposal_bytes = max_proposal_bytes
        self.executor_options = executor_options

    def capabilities(self) -> HarnessCaps:
        return HarnessCaps(native_tools=True, external_process=False, structured_output=True)

    # -- phase helpers -----------------------------------------------------

    async def _chat_one(self, model: str, messages: list[dict[str, Any]], timeout: float) -> str:
        """One plain chat call; returns '' on timeout/error (degrade, never block)."""
        try:
            result = await asyncio.wait_for(
                self.backend.chat(messages=messages, model=model), timeout=timeout
            )
        except Exception as exc:  # noqa: BLE001 -- degrade per model
            return f"[failed: {exc}]"[:200]
        return (result.content or "")[: self.max_proposal_bytes]

    async def _phase(
        self,
        prompts: dict[str, str],
        system: str,
        timeout: float,
    ) -> dict[str, str]:
        """Run one plain-text phase for every model in parallel."""
        async def one(model: str, prompt: str) -> tuple[str, str]:
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ]
            return model, await self._chat_one(model, messages, timeout)

        results = await asyncio.gather(*(one(m, p) for m, p in prompts.items()))
        return dict(results)

    @staticmethod
    def _pick_winner(judge_text: str, proposals: dict[str, str], fallback: str) -> str:
        match = _WINNER_RE.search(judge_text or "")
        if match:
            winner = match.group(1).strip()
            if winner in proposals:
                return winner
        return fallback

    # -- main --------------------------------------------------------------

    async def run(self, prompt: str, workspace: Path, *, timeout_s: int = 600) -> HarnessResult:
        workspace = Path(workspace)
        workspace.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + max(timeout_s, 30)
        user_prompt = build_user_prompt(Role.CODER, {"task": prompt})
        system = SYSTEM_PROMPTS[Role.CODER]

        # Phase 1: proposals (parallel, plain chat).
        left = max(deadline - time.monotonic(), 5.0)
        proposals = await self._phase(
            {m: user_prompt for m in self.models}, system, timeout=left * 0.4
        )
        proposals = {m: t for m, t in proposals.items() if t and not t.startswith("[failed:")}
        if not proposals:
            return HarnessResult(ok=False, error="discuss harness: no valid proposals")

        # Phase 2: one critique round.
        transcript = "\n\n".join(f"### Proposal by {m}\n{t}" for m, t in proposals.items())
        critique_prompt = (
            f"Task:\n{prompt}\n\nPeer proposals:\n{transcript}\n\n"
            "Critique each proposal and say which is strongest for the task."
        )
        left = max(deadline - time.monotonic(), 5.0)
        critiques = await self._phase(
            {m: critique_prompt for m in proposals}, system, timeout=left * 0.4
        )

        # Phase 3: judge picks the winner.
        judged = "\n\n".join(
            f"### Proposal by {m}\n{t}\n\nCritique by {m}: {critiques.get(m, '')}"
            for m, t in proposals.items()
        )
        judge_prompt = (
            f"Task:\n{prompt}\n\nProposals and critiques:\n{judged}\n\n"
            "Pick the strongest proposal for the task. Answer with a line "
            "'WINNER: <model name>' followed by a short rationale."
        )
        left = max(deadline - time.monotonic(), 5.0)
        judge_text = await self._chat_one(
            self.judge_model,
            [{"role": "user", "content": judge_prompt}],
            timeout=left * 0.3,
        )
        winner = self._pick_winner(judge_text, proposals, fallback=next(iter(proposals)))
        rationale = (judge_text or "").strip().replace("\n", " ")[:200]

        # Phase 4: execute the winner's plan via the in-proc tool loop.
        left = max(deadline - time.monotonic(), 30.0)
        executor = ToolLoopHarness(
            model=winner,
            backend=self.backend,
            tools=self.tools,
            execute_tool=self.execute_tool,
            **self.executor_options,
        )
        result = await executor.run(prompt, workspace, timeout_s=int(left))
        if result.ok:
            result.summary = f"[discuss] winner={winner}; {rationale}".strip()
        return result
