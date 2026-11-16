"""DiscussHarness tests (14.14 item 4): debate phases + worker wiring.

Sync tests + asyncio.run, no pytest.mark decorators (content-filter rule).
"""

import asyncio
import json
import subprocess

from test_harnesses import _coder_envelope, _FakeBus, _FakeChatResult, _FakeTools

from memnc.config import Settings
from memnc.harnesses import _REGISTRY, HarnessResult, known_harnesses
from memnc.harnesses.discuss import DiscussHarness
from memnc.worker import Worker

_FINAL = {"summary": "wrote hi.py", "files": [{"path": "hi.py", "content": "x = 1\n"}]}


class _DispatchBackend:
    """Answers by phase marker in the last message; after the judge, tool loop.

    Phase order: proposals (one per model) -> critiques (one per model) ->
    judge -> ToolLoopHarness (first a write_file tool call, then final JSON).
    Distinguishing executor calls from proposal calls: the executor is only
    reached after the judge call has been seen (``self.judged``).
    """

    def __init__(self, proposals, critique="critique text", judge="WINNER: strong"):
        self.proposals = proposals
        self.critique = critique
        self.judge = judge
        self.judged = False
        self.wrote = False
        self.calls = []

    async def chat(self, messages, tools=None, model=None, **kwargs):
        prompt = messages[-1]["content"]
        self.calls.append((model, prompt[:40]))
        if "Pick the strongest" in prompt:
            self.judged = True
            return _FakeChatResult(content=self.judge)
        if "Peer proposals" in prompt:
            return _FakeChatResult(content=self.critique)
        if self.judged:
            if not self.wrote:
                self.wrote = True
                call = type("TC", (), {"name": "write_file", "arguments": {"path": "hi.py"},
                                       "id": "c1"})()
                return _FakeChatResult(tool_calls=[call])
            return _FakeChatResult(content=json.dumps(_FINAL))
        return _FakeChatResult(content=self.proposals[model])


class _EmptyBackend:
    async def chat(self, messages, **kwargs):
        return _FakeChatResult(content="")


def _runner(tmp_path, backend, models, judge="WINNER: strong"):
    return DiscussHarness(
        model=models[0],
        backend=backend,
        discuss_models=models,
        judge_model=judge or "",
        execute_tool=lambda n, a: asyncio.sleep(0, result="ok"),
    )


def test_discuss_harness_registered():
    assert "discuss" in known_harnesses()
    caps = DiscussHarness(model="a", backend=None, discuss_models=["a", "b"]).capabilities()
    assert caps.external_process is False


def test_discuss_debate_executes_winner(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    backend = _DispatchBackend({"weak": "plan one", "strong": "plan two"},
                               judge="WINNER: strong\nrationale here")
    harness = _runner(tmp_path, backend, ["weak", "strong"])
    result = asyncio.run(harness.run("write hi.py", tmp_path, timeout_s=30))
    assert result.ok is True
    assert result.summary.startswith("[discuss] winner=strong")
    assert result.tool_events == 1
    assert (tmp_path / "hi.py").read_text() == "x = 1\n"
    # 2 proposals + 2 critiques + 1 judge + 2 tool-loop calls
    assert len(backend.calls) == 7


def test_discuss_judge_garbage_falls_back(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    backend = _DispatchBackend({"m1": "plan one", "m2": "plan two"}, judge="no winner line")
    result = asyncio.run(_runner(tmp_path, backend, ["m1", "m2"]).run(
        "write hi.py", tmp_path, timeout_s=30))
    assert result.ok is True
    assert result.summary.startswith("[discuss] winner=m1")


def test_discuss_judge_names_unknown_model_falls_back(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    backend = _DispatchBackend({"m1": "plan one"}, judge="WINNER: nonexistent-model")
    result = asyncio.run(_runner(tmp_path, backend, ["m1"]).run(
        "write hi.py", tmp_path, timeout_s=30))
    assert result.ok is True
    assert result.summary.startswith("[discuss] winner=m1")


def test_discuss_no_valid_proposals_fails(tmp_path):
    harness = DiscussHarness(model="m1", backend=_EmptyBackend(), discuss_models=["m1"])
    result = asyncio.run(harness.run("task", tmp_path, timeout_s=5))
    assert result.ok is False
    assert "no valid proposals" in result.error


def test_discuss_empty_models_falls_back_to_model(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    backend = _DispatchBackend({"solo": "plan"}, judge="")
    result = asyncio.run(_runner(tmp_path, backend, ["solo"]).run(
        "write hi.py", tmp_path, timeout_s=30))
    assert result.ok is True
    assert result.summary.startswith("[discuss] winner=solo")


def test_worker_wiring_passes_backend_and_tools(tmp_path):
    captured = {}
    worker = Worker(
        Settings(role="coder", workspace_root=str(tmp_path / "ws"), mcp_enabled=False,
                 harness_coder="discuss", discuss_models=["m1", "m2"], judge_model="m2"),
        backend=None, tools=_FakeTools(), bus=_FakeBus(),
    )

    class Recorder:
        name = "discuss-recorder"

        def __init__(self, **kwargs):
            captured.update(kwargs)

        def capabilities(self):
            return {}

        async def run(self, prompt, workspace, timeout_s=900):
            return HarnessResult(ok=True, summary="done", tool_events=0)

    saved = _REGISTRY.get("discuss")
    _REGISTRY["discuss"] = Recorder
    try:
        result = asyncio.run(worker._run(_coder_envelope({"task": "x"}), None))
    finally:
        if saved is None:
            _REGISTRY.pop("discuss", None)
        else:
            _REGISTRY["discuss"] = saved
    assert result.ok is True
    assert captured.get("backend") is None  # worker backend is None in the fixture
    assert captured.get("discuss_models") == ["m1", "m2"]
    assert captured.get("judge_model") == "m2"
    assert callable(captured.get("execute_tool"))
