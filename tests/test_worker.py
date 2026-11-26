import pytest

from memnc.config import Settings
from memnc.schema import CoderOutput, Envelope, TaskSnapshot
from memnc.worker import (
    Worker,
    merge_written_files,
    python_syntax_errors,
    tool_key,
)


class FakeBus:
    def __init__(self):
        self.snapshots = {}

    async def get_snapshot(self, task_id):
        return self.snapshots.get(task_id)

    async def put_snapshot(self, snapshot):
        self.snapshots[snapshot.task_id] = snapshot.model_copy(deep=True)

    async def connect(self):
        return None

    async def close(self):
        return None


class FakeTools:
    available = True
    schemas = []

    def __init__(self):
        self.calls = []

    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        return "result"

    async def close(self):
        return None


def make_worker(tmp_path, bus):
    settings = Settings(role="coder", workspace_root=tmp_path, mcp_enabled=False)
    return Worker(settings, backend=None, tools=FakeTools(), bus=bus)


def test_tool_key_is_stable_and_sensitive():
    assert tool_key("c1", "read", {"a": 1}) == tool_key("c1", "read", {"a": 1})
    assert tool_key("c1", "read", {"a": 1}) != tool_key("c2", "read", {"a": 1})
    assert tool_key("c1", "read", {"a": 1}) != tool_key("c1", "read", {"a": 2})


def test_python_syntax_errors_reports_only_broken_python():
    errors = python_syntax_errors(
        {"good.py": "x = 1\n", "bad.py": "if __name__ == $1):\n", "notes.txt": "x"}
    )
    assert len(errors) == 1
    assert errors[0].startswith("bad.py:")


def test_merge_written_files_prefers_json_artifacts():
    output = CoderOutput.model_validate(
        {
            "summary": "",
            "files": [{"path": "a.py", "content": "new"}],
            "commands": [],
        }
    )
    merged = merge_written_files(output, {"a.py": "old", "b.py": "tool"})
    assert merged == {"a.py": "new", "b.py": "tool"}


def test_note_written_tracks_only_successful_writes(tmp_path):
    worker = make_worker(tmp_path, FakeBus())
    worker._note_written("write_file", {"path": "a.py", "content": "x = 1"}, '{"ok": true}')
    worker._note_written("write_file", {"path": "b.py", "content": "y"}, '{"ok": false}')
    worker._note_written("read_file", {"path": "c.py"}, '{"ok": true}')
    assert worker._written_files == {"a.py": "x = 1"}


@pytest.mark.asyncio
async def test_tool_results_are_replayed_not_reexecuted(tmp_path):
    bus = FakeBus()
    worker = make_worker(tmp_path, bus)
    snapshot = TaskSnapshot(task_id="t1", trace_id="tr1", task="x")
    bus.snapshots["t1"] = snapshot
    envelope = Envelope(id="cmd1", task_id="t1", trace_id="tr1", kind="command", role="coder")

    first = await worker._execute_tool(envelope, "read_file", {"path": "a"})
    second = await worker._execute_tool(envelope, "read_file", {"path": "a"})

    assert first == second == "result"
    assert len(worker.tools.calls) == 1
    stored = bus.snapshots["t1"]
    assert len(stored.completed_tool_calls) == 1


@pytest.mark.asyncio
async def test_tool_results_from_previous_command_are_reexecuted(tmp_path):
    bus = FakeBus()
    worker = make_worker(tmp_path, bus)
    bus.snapshots["t1"] = TaskSnapshot(task_id="t1", trace_id="tr1", task="x")

    await worker._execute_tool(
        Envelope(id="cmd1", task_id="t1", trace_id="tr1", kind="command"), "t", {}
    )
    await worker._execute_tool(
        Envelope(id="cmd2", task_id="t1", trace_id="tr1", kind="command"), "t", {}
    )
    assert len(worker.tools.calls) == 2


def test_verify_gate_rejects_paths_outside_workspace(tmp_path):
    import asyncio
    from types import SimpleNamespace

    from memnc.worker import Worker

    outside = tmp_path / "outside.py"
    outside.write_text("print('x')\n")
    ws = tmp_path / "ws"
    ws.mkdir()
    worker = Worker.__new__(Worker)
    worker.settings = SimpleNamespace(workspace_root=str(ws))
    critique = SimpleNamespace(approved=False, summary="", issues=[])
    snap = SimpleNamespace(
        critique=critique,
        code=SimpleNamespace(files=[SimpleNamespace(path=str(outside))]),
    )
    asyncio.run(worker._verify_gate(snap))
    assert critique.approved is False
    assert "REJECTED" in critique.issues[0].issue


def test_mesh_submit_reports_orchestrator_rejection_as_failure():
    import asyncio
    import json
    from types import SimpleNamespace

    from memnc.worker import Worker

    class _NC:
        async def request(self, subject, payload, timeout):
            reply = {"error": "max_submit_depth 3 reached"}
            return SimpleNamespace(data=json.dumps(reply).encode())

    class _Bus:
        nc = _NC()

        async def get_snapshot(self, task_id):
            return None

    worker = Worker.__new__(Worker)
    worker.bus = _Bus()
    worker.settings = SimpleNamespace(max_submit_depth=3, node_name="node-a", mesh_claim=False)
    out = json.loads(
        asyncio.run(worker._mesh_submit(SimpleNamespace(task_id="t1"), {"task": "do it"}))
    )
    assert out["ok"] is False
    assert "max_submit_depth" in out["error"]
    from types import SimpleNamespace

    from memnc.worker import Worker


def test_command_for_finished_task_is_acked_without_running():
    import asyncio
    from types import SimpleNamespace

    from memnc.schema import Envelope, TaskSnapshot, TaskState
    from memnc.worker import Worker

    class _Bus:
        async def get_snapshot(self, task_id):
            return TaskSnapshot(task_id=task_id, trace_id="tr", task="x", state=TaskState.FAILED)

        async def dead_letter(self, *args, **kwargs):
            raise AssertionError("unexpected dead letter")

    acked: list[bool] = []

    class _Msg:
        data = Envelope(id="cmd1", task_id="t1", trace_id="tr", kind="command").model_dump_json()

        async def ack(self):
            acked.append(True)

        async def nak(self, delay=None):
            raise AssertionError("unexpected nak")

    async def _run(*args, **kwargs):
        raise AssertionError("command ran for a finished task")

    worker = Worker.__new__(Worker)
    worker.bus = _Bus()
    worker.settings = SimpleNamespace(prefer_node="", node_name="node-a-coder", enforce_node_version=False)
    worker._run = _run  # type: ignore[method-assign]
    asyncio.run(worker._handle(_Msg()))
    assert acked == [True]
