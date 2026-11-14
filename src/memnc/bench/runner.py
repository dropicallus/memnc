"""Bench runner: 3-arm swarm-vs-single benchmark (roadmap 14.15.9, stage 2).

Arms (per benchmark design, committed in roadmap 14.15.9):
  A) single shot  — coder model, one chance, no plan, no critique.
  B) self-reflect — coder model plus up to 3 self-reflection rounds
                    (same model critiques its own output and revises).
  C) mesh         — planner -> coder -> critic loop (max 3 revisions),
                    mirroring the production FSM role set.

The deterministic bench checkers are NEVER shown to the models and never
terminate a loop early: the benchmark measures what the pipeline achieves on
its own; the checker only scores the final workspace once (checker leakage
would turn arm C into "arm A plus an oracle").

Reps are seeded via a fixed temperature ladder (deterministic, no RNG);
the prompt is identical across reps, only sampling temperature varies.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from memnc.bench import BenchTask, load_tasks, verify_task
from memnc.config import Settings
from memnc.roles import PROMPT_VERSION, Role, build_user_prompt

log = logging.getLogger(__name__)

MAX_REVISIONS = 3
REFLECTION_ROUNDS = 3
# Seeded temperature ladder: one entry per rep, identical across arms.
REP_TEMPERATURES = (0.0, 0.2, 0.4, 0.6, 0.8)

ARMS = ("A", "B", "C")
ARM_NAMES = {"A": "single-shot", "B": "self-reflect", "C": "mesh"}

CODER_SELF_CRITIQUE_PROMPT = (
    "You are reviewing your OWN implementation against the task. "
    "Do not rewrite code yet. Identify concrete defects by inspecting the "
    "files you produced (mentally execute them). Reply with ONLY a JSON "
    'object: {"approved": true|false, "issues": ["..."]}. '
    "Approve only if the implementation is correct and complete. "
    "No prose, no markdown fences."
)


# --------------------------------------------------------------------------
# Output parsing (tolerant: models add fences / prose despite instructions)
# --------------------------------------------------------------------------

def extract_json(text: str) -> dict[str, Any] | None:
    """Return the first JSON object found in ``text`` or None."""
    if not text:
        return None
    candidates: list[str] = []
    stripped = text.strip()
    if stripped.startswith("```"):
        body = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        if body.rstrip().endswith("```"):
            body = body.rstrip()[:-3]
        candidates.append(body)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    candidates.append(text)
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _files_from_payload(data: dict[str, Any] | None) -> dict[str, str]:
    """Extract {relative path: content} from a coder-style payload."""
    files: dict[str, str] = {}
    if not data:
        return files
    for item in data.get("files") or []:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()
        content = item.get("content")
        if path and isinstance(content, str):
            files[path] = content
    return files


# --------------------------------------------------------------------------
# Result records
# --------------------------------------------------------------------------

@dataclass
class RunResult:
    run_id: str
    arm: str
    task_id: str
    tier: str
    rep: int
    success: bool = False
    issues: list[str] = field(default_factory=list)
    llm_calls: int = 0
    revisions: int = 0
    elapsed_s: float = 0.0
    output_files: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    prompt_version: str = PROMPT_VERSION
    error: str = ""


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------

class BenchEngine:
    """Executes bench tasks under one of the three arms, fully inproc."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()
        from memnc.backends import make_backend

        self.backend = make_backend(self.settings)

    async def close(self) -> None:
        await self.backend.close()

    async def _chat(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        temperature: float,
    ) -> tuple[dict[str, Any] | None, str, int, int]:
        """One chat turn. Returns (parsed payload, raw content, tok_in, tok_out)."""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        result = await self.backend.chat(
            messages, tools=None, model=model, temperature=temperature
        )
        content = result.content
        usage = result.raw.get("usage") or {}
        tokens_in = int(usage.get("prompt_tokens") or result.raw.get("prompt_eval_count") or 0)
        tokens_out = int(usage.get("completion_tokens") or result.raw.get("eval_count") or 0)
        return extract_json(content), content, tokens_in, tokens_out

    def _model(self, role: Role) -> str:
        return self.settings.model_for(role)

    async def _coder_pass(
        self,
        task: BenchTask,
        temperature: float,
        extra_payload: dict[str, Any] | None = None,
    ) -> tuple[dict[str, str], int, int]:
        """One coder turn. Returns (files, tokens_in, tokens_out)."""
        payload: dict[str, Any] = {"task": task.prompt}
        if extra_payload:
            payload.update(extra_payload)
        from memnc.roles import SYSTEM_PROMPTS

        data, _raw, t_in, t_out = await self._chat(
            SYSTEM_PROMPTS[Role.CODER],
            build_user_prompt(Role.CODER, payload),
            self._model(Role.CODER),
            temperature,
        )
        return _files_from_payload(data), t_in, t_out

    async def run_task(
        self,
        task: BenchTask,
        arm: str,
        rep: int,
        workroot: Path,
    ) -> RunResult:
        """Run one (task, arm, rep) combination and verify the workspace."""
        assert arm in ARMS, f"unknown arm {arm!r}"
        temperature = REP_TEMPERATURES[rep % len(REP_TEMPERATURES)]
        run_id = f"{task.id}__{arm}{rep}"
        result = RunResult(
            run_id=run_id, arm=arm, task_id=task.id, tier=task.tier, rep=rep
        )
        workdir = workroot / run_id
        started = time.monotonic()
        try:
            if arm == "A":
                files, t_in, t_out = await self._coder_pass(task, temperature)
                result.llm_calls = 1
                result.tokens_in += t_in
                result.tokens_out += t_out
                result.output_files = len(files)
                self._materialize(workdir, files)
            elif arm == "B":
                files, t_in, t_out = await self._coder_pass(task, temperature)
                result.llm_calls += 1
                result.tokens_in += t_in
                result.tokens_out += t_out
                result.output_files = len(files)
                self._materialize(workdir, files)
                for _round_no in range(1, REFLECTION_ROUNDS + 1):
                    verdict, t_in, t_out = await self._self_critique(
                        task, files, temperature
                    )
                    result.llm_calls += 1
                    result.tokens_in += t_in
                    result.tokens_out += t_out
                    if verdict.get("approved"):
                        break
                    files, t_in, t_out = await self._coder_pass(
                        task,
                        temperature,
                        extra_payload={
                            "context": "SELF-CRITIQUE to address: "
                            + json.dumps(verdict.get("issues", []))
                        },
                    )
                    result.llm_calls += 1
                    result.tokens_in += t_in
                    result.tokens_out += t_out
                    result.revisions += 1
                    result.output_files = len(files)
                    self._materialize(workdir, files)
            else:  # arm == "C"
                files = await self._run_mesh_arm(task, temperature, result)
                self._materialize(workdir, files)
        except Exception as exc:  # noqa: BLE001 - a failed arm run is a datapoint
            log.warning("bench run %s failed: %s", run_id, exc)
            result.error = f"{type(exc).__name__}: {exc}"
        result.elapsed_s = round(time.monotonic() - started, 2)
        issues = verify_task(task, workdir) if workdir.is_dir() else ["no workspace"]
        result.issues = issues
        result.success = not issues and not result.error
        return result

    async def _self_critique(
        self,
        task: BenchTask,
        files: dict[str, str],
        temperature: float,
    ) -> tuple[dict[str, Any], int, int]:
        """Coder model critiques its own output (arm B reflection round)."""

        listing = "\n\n".join(f"--- {p} ---\n{c}" for p, c in files.items())
        payload = {
            "task": task.prompt,
            "code": {"summary": "self-review", "files": self._file_entries(files)},
        }
        user = build_user_prompt(Role.CRITIC, payload) + "\n\nYOUR OUTPUT FILES:\n" + listing
        data, _raw, t_in, t_out = await self._chat(
            CODER_SELF_CRITIQUE_PROMPT, user, self._model(Role.CODER), temperature
        )
        if data is None:
            data = {"approved": False, "issues": ["unparseable critique output"]}
        return data, t_in, t_out

    async def _run_mesh_arm(
        self,
        task: BenchTask,
        temperature: float,
        result: RunResult,
    ) -> dict[str, str]:
        """Arm C: planner -> coder -> critic loop, mirroring the FSM."""
        from memnc.roles import SYSTEM_PROMPTS

        # 1) planner
        plan_data, _raw, t_in, t_out = await self._chat(
            SYSTEM_PROMPTS[Role.PLANNER],
            build_user_prompt(Role.PLANNER, {"task": task.prompt}),
            self._model(Role.PLANNER),
            temperature,
        )
        result.llm_calls += 1
        result.tokens_in += t_in
        result.tokens_out += t_out
        plan = plan_data if isinstance(plan_data, dict) else None

        # 2) coder first pass (with plan as context)
        files, t_in, t_out = await self._coder_pass(
            task, temperature, extra_payload={"plan": plan} if plan else None
        )
        result.llm_calls += 1
        result.tokens_in += t_in
        result.tokens_out += t_out
        result.output_files = len(files)

        # 3) critic loop (max MAX_REVISIONS revisions, first approval wins)
        for revision in range(1, MAX_REVISIONS + 1):
            critique, t_in, t_out = await self._critic_call(
                task, plan, files, temperature
            )
            result.llm_calls += 1
            result.tokens_in += t_in
            result.tokens_out += t_out
            if critique.get("approved"):
                break
            revised, t_in, t_out = await self._coder_pass(
                task,
                temperature,
                extra_payload={
                    "plan": plan,
                    "code": {"summary": "revision", "files": self._file_entries(files)},
                    "critique": critique,
                    "revisions": revision,
                },
            )
            result.llm_calls += 1
            result.tokens_in += t_in
            result.tokens_out += t_out
            result.revisions += 1
            if revised:
                files = revised
                result.output_files = len(files)
        return files

    async def _critic_call(
        self,
        task: BenchTask,
        plan: dict[str, Any] | None,
        files: dict[str, str],
        temperature: float,
    ) -> tuple[dict[str, Any], int, int]:
        from memnc.roles import SYSTEM_PROMPTS

        payload: dict[str, Any] = {
            "task": task.prompt,
            "code": {"summary": "candidate", "files": self._file_entries(files)},
        }
        if plan:
            payload["plan"] = plan
        data, _raw, t_in, t_out = await self._chat(
            SYSTEM_PROMPTS[Role.CRITIC],
            build_user_prompt(Role.CRITIC, payload),
            self._model(Role.CRITIC),
            temperature,
        )
        if data is None:
            data = {"approved": False, "summary": "unparseable critic output", "issues": []}
        return data, t_in, t_out

    @staticmethod
    def _file_entries(files: dict[str, str]) -> list[dict[str, str]]:
        return [{"path": p, "content": c} for p, c in files.items()]

    def _materialize(self, workdir: Path, files: dict[str, str]) -> None:
        if workdir.exists():
            shutil.rmtree(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        for rel, content in files.items():
            rel = rel.lstrip("./")
            target = (workdir / rel).resolve()
            # keep writes inside the run workspace
            if not str(target).startswith(str(workdir.resolve())):
                log.warning("refusing path escape %r", rel)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")


# --------------------------------------------------------------------------
# Suite driver + report
# --------------------------------------------------------------------------

def build_suite(arms: list[str], reps: int, tiers: list[str] | None = None) -> list[dict[str, Any]]:
    """Cross product arms x tasks x reps in deterministic order."""
    suite: list[dict[str, Any]] = []
    tasks = load_tasks(tiers[0] if tiers and len(tiers) == 1 else None)
    if tiers and len(tiers) > 1:
        tasks = [t for t in tasks if t.tier in tiers]
    for arm in arms:
        for task in tasks:
            for rep in range(min(reps, len(REP_TEMPERATURES))):
                suite.append({"arm": arm, "task": task, "rep": rep})
    return suite


def wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval; returns (low, high) as fractions."""
    if total == 0:
        return (0.0, 0.0)
    p = successes / total
    z2 = z * z
    denom = 1 + z2 / total
    centre = (p + z2 / (2 * total)) / denom
    spread = z * ((p * (1 - p) / total + z2 / (4 * total * total)) ** 0.5) / denom
    return (max(0.0, centre - spread), min(1.0, centre + spread))


def write_report(results: list[RunResult], path: Path) -> str:
    """Render bench-report.md and return it as text."""
    def group(rows: list[RunResult]) -> dict[str, list[RunResult]]:
        out: dict[str, list[RunResult]] = {}
        for row in rows:
            out.setdefault(row.arm, []).append(row)
        return out

    lines: list[str] = [
        "# bench-report: swarm vs single (roadmap 14.15.9)",
        "",
        f"Runs: {len(results)} | prompt version: {PROMPT_VERSION}",
        "",
        "| arm | tier | runs | success | rate | 95% CI | mean rounds | mean tok (in/out) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    overall = group(results)
    for arm in ARMS:
        rows = overall.get(arm, [])
        if not rows:
            continue
        tiers_seen: list[str] = sorted({r.tier for r in rows})
        for tier in tiers_seen + ["all"]:
            subset = rows if tier == "all" else [r for r in rows if r.tier == tier]
            ok = sum(1 for r in subset if r.success)
            low, high = wilson_interval(ok, len(subset))
            rounds = sum(r.revisions for r in subset) / len(subset)
            tok = sum(r.tokens_in + r.tokens_out for r in subset) / len(subset)
            lines.append(
                f"| {arm} ({ARM_NAMES[arm]}) | {tier} | {len(subset)} | {ok} "
                f"| {ok / len(subset):.0%} | [{low:.0%}, {high:.0%}] "
                f"| {rounds:.2f} | {tok:.0f} |"
            )
    errors = [r for r in results if r.error]
    if errors:
        lines += ["", f"## errors ({len(errors)})", ""]
        for row in errors[:20]:
            lines.append(f"- {row.run_id}: {row.error}")
    failing = [r for r in results if not r.success and not r.error]
    if failing:
        lines += ["", "## failing checks (sample)", ""]
        for row in failing[:20]:
            lines.append(f"- {row.run_id}: {'; '.join(row.issues[:2])}")
    text = "\n".join(lines) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return text


async def run_suite(
    arms: list[str],
    reps: int,
    tiers: list[str] | None,
    out_path: Path,
    report_path: Path | None,
    limit: int | None = None,
) -> list[RunResult]:
    """Execute the suite sequentially, appending each result to a JSONL file."""
    suite = build_suite(arms, reps, tiers)
    if limit:
        suite = suite[:limit]
    engine = BenchEngine()
    workroot = Path(tempfile.gettempdir()) / "memnc-bench" / time.strftime("%Y%m%d-%H%M%S")
    results: list[RunResult] = []
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        for i, spec in enumerate(suite, start=1):
            print(f"[{i}/{len(suite)}] arm {spec['arm']} rep {spec['rep']} "
                  f"{spec['task'].id} ({spec['task'].tier}) ...", flush=True)
            row = await engine.run_task(spec["task"], spec["arm"], spec["rep"], workroot)
            results.append(row)
            with out_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(asdict(row)) + "\n")
            print(
                f"    -> {'PASS' if row.success else 'FAIL'}"
                f" ({row.elapsed_s}s, {row.llm_calls} calls)", flush=True,
            )
    finally:
        await engine.close()
        if report_path is not None and results:
            write_report(results, report_path)
            print(f"report written: {report_path}", flush=True)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arms", default="A,B,C", help="comma list of A,B,C")
    parser.add_argument("--reps", type=int, default=5)
    parser.add_argument("--tiers", default="", help="comma list easy,medium,hard")
    parser.add_argument("--out", default="bench-results.jsonl")
    parser.add_argument("--report", default="bench-report.md")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)
    arms = [a.strip().upper() for a in args.arms.split(",") if a.strip()]
    tiers = [t.strip() for t in args.tiers.split(",") if t.strip()] or None
    results = asyncio.run(
        run_suite(
            arms=arms,
            reps=args.reps,
            tiers=tiers,
            out_path=Path(args.out),
            report_path=Path(args.report),
            limit=args.limit,
        )
    )
    ok = sum(1 for r in results if r.success)
    print(f"done: {ok}/{len(results)} passed")


if __name__ == "__main__":
    main()