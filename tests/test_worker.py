from pathlib import Path

import pytest

from memnc.config import Settings
from memnc.schema import CoderOutput, Envelope, TaskSnapshot, TaskState
from memnc.worker import (
    Worker,
    merge_written_files,
    python_syntax_errors,
    tool_key,
)


class FakeBus:
    def __init__(self):
        self.snapshots = {}
        self.tool_caches = {}

    async def get_snapshot(self, task_id):
        return self.snapshots.get(task_id)

    async def put_snapshot(self, snapshot):
        self.snapshots[snapshot.task_id] = snapshot.model_copy(deep=True)

    async def get_tool_cache(self, task_id, command_id):
        return dict(self.tool_caches.get((task_id, command_id), {}))

    async def put_tool_cache(self, task_id, command_id, results):
        self.tool_caches[(task_id, command_id)] = dict(results)

    async def delete_tool_cache(self, task_id, command_id):
        self.tool_caches.pop((task_id, command_id), None)

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
    worker._note_written(
        "write_file", {"path": "a.py", "content": "x = 1"}, '{"ok": true}'
    )
    worker._note_written(
        "write_file", {"path": "b.py", "content": "y"}, '{"ok": false}'
    )
    worker._note_written("read_file", {"path": "c.py"}, '{"ok": true}')
    assert worker._written_files == {"a.py": "x = 1"}


@pytest.mark.asyncio
async def test_tool_results_are_replayed_not_reexecuted(tmp_path):
    bus = FakeBus()
    worker = make_worker(tmp_path, bus)
    snapshot = TaskSnapshot(task_id="t1", trace_id="tr1", task="x")
    bus.snapshots["t1"] = snapshot
    envelope = Envelope(
        id="cmd1", task_id="t1", trace_id="tr1", kind="command", role="coder"
    )

    first = await worker._execute_tool(envelope, "read_file", {"path": "a"})
    second = await worker._execute_tool(envelope, "read_file", {"path": "a"})

    assert first == second == "result"
    assert len(worker.tools.calls) == 1
    cache = bus.tool_caches[("t1", "cmd1")]
    assert len(cache) == 1


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
