"""End-to-end workflow against a real NATS server.

Enabled only when MEMNC_TEST_NATS is set, e.g.:

    docker run -d --rm -p 4223:4222 nats:2-alpine -js
    MEMNC_TEST_NATS=nats://127.0.0.1:4223 pytest -m integration
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import nats
import pytest

from memnc.backends import ChatResult
from memnc.bus import Bus
from memnc.config import Settings
from memnc.orchestrator import Orchestrator
from memnc.schema import Role, SUBJECT_SUBMIT, TaskSubmission, TaskState
from memnc.worker import Worker

NATS_URL = os.environ.get("MEMNC_TEST_NATS")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not NATS_URL, reason="MEMNC_TEST_NATS not set"),
]


class ScriptedBackend:
    def __init__(self, contents: list[str]):
        self.contents = list(contents)

    async def chat(self, messages, tools=None, model="", **options):
        if not self.contents:
            raise AssertionError("scripted backend exhausted")
        return ChatResult(content=self.contents.pop(0))

    async def list_models(self):
        return []

    async def close(self):
        return None


def planner_output() -> str:
    return json.dumps(
        {
            "plan": [{"step": 1, "description": "create a.py", "files": ["a.py"]}],
            "notes": "trivial",
        }
    )


def coder_output(value: str) -> str:
    return json.dumps(
        {
            "summary": f"write {value}",
            "files": [{"path": "a.py", "content": value}],
            "commands": [],
        }
    )


def critic_output(approved: bool) -> str:
    return json.dumps(
        {
            "approved": approved,
            "summary": "looks good" if approved else "needs work",
            "issues": []
            if approved
            else [{"file": "a.py", "severity": "major", "issue": "wrong value"}],
        }
    )


async def wait_for_state(bus: Bus, task_id: str, timeout: float = 30.0):
    deadline = asyncio.get_running_loop().time() + timeout
    last = None
    while asyncio.get_running_loop().time() < deadline:
        last = await bus.get_snapshot(task_id)
        if last is not None and last.state in {
            TaskState.DONE,
            TaskState.FAILED,
            TaskState.CANCELLED,
        }:
            return last
        await asyncio.sleep(0.2)
    raise AssertionError(f"task did not finish, last state={last}")


@pytest.mark.asyncio
async def test_full_workflow_with_revision(tmp_path):
    suffix = uuid.uuid4().hex[:8]
    base = Settings(
        nats_url=NATS_URL,
        node_name="test-node",
        workspace_root=tmp_path,
        stream_name=f"MESH_TEST_{suffix}",
        kv_bucket=f"workflow_test_{suffix}",
        mcp_enabled=False,
        max_revisions=2,
        max_attempts=2,
        planner_timeout_s=15,
        coder_timeout_s=15,
        critic_timeout_s=15,
        orchestrator_grace_s=2,
    )

    orchestrator = Orchestrator(base, Bus(base))
    workers = []
    backends = {
        Role.PLANNER: ScriptedBackend([planner_output()]),
        Role.CODER: ScriptedBackend([coder_output("first"), coder_output("second")]),
        Role.CRITIC: ScriptedBackend([critic_output(False), critic_output(True)]),
    }
    for role in Role:
        settings = base.model_copy(update={"role": role})
        workers.append(Worker(settings, backend=backends[role], tools=None, bus=Bus(settings)))

    tasks = [asyncio.create_task(orchestrator.start())]
    tasks += [asyncio.create_task(worker.start()) for worker in workers]

    checker = Bus(base)
    client = None
    try:
        await asyncio.sleep(1.0)
        client = await nats.connect(NATS_URL)
        response = await client.request(
            SUBJECT_SUBMIT,
            TaskSubmission(task="write a.py").model_dump_json().encode(),
            timeout=10,
        )
        data = json.loads(response.data)
        assert "task_id" in data, data

        await checker.connect()
        snapshot = await wait_for_state(checker, data["task_id"])

        assert snapshot.state is TaskState.DONE, snapshot.error
        assert snapshot.revisions == 1
        assert snapshot.plan is not None
        assert snapshot.code is not None
        assert snapshot.critique is not None
        assert snapshot.critique.approved is True
        assert (tmp_path / "a.py").read_text() == "second"
    finally:
        if client is not None:
            await client.close()
        await orchestrator.stop()
        for worker in workers:
            await worker.stop()
        await asyncio.gather(*tasks, return_exceptions=True)
        await orchestrator.aclose()
        for worker in workers:
            await worker.aclose()
        try:
            assert checker.js is not None
            await checker.js.delete_stream(base.stream_name)
            await checker.js.delete_key_value(base.kv_bucket)
        except Exception:  # noqa: BLE001 - cleanup best effort
            pass
        await checker.close()
