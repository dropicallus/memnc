"""Tests for experience-store producer wiring (plan section 15.3, item 5)."""

import asyncio
import json
import os

from test_observations import _RecordingBus  # has deposit_observation

from memnc.config import Settings
from memnc.experience_store import ExperienceStore, format_bullets, record_observation
from memnc.observations import TaskObservation
from memnc.orchestrator import Orchestrator
from memnc.schema import TaskSnapshot, TaskState


def _obs(task_id: str, outcome: str, route: dict[str, str], error: str = "") -> TaskObservation:
    return TaskObservation(
        task_id=task_id,
        trace_id=f"tr-{task_id}",
        task="write a function",
        outcome=outcome,
        route=route,
        error=error,
        revisions=2,
    )


class TestRecordObservation:
    def test_success_goes_to_success_topic(self, tmp_path) -> None:
        store = ExperienceStore(str(tmp_path / "exp.json"))
        added = record_observation(store, _obs("aaaaaaaa-1", "done", {"coder": "n:llama"}))
        assert added and "DONE" in added[0] and "n:llama" in added[0]
        assert [b.topic for b in store.bullets] == ["success"]

    def test_failure_goes_to_failure_topic_with_error(self, tmp_path) -> None:
        store = ExperienceStore(str(tmp_path / "exp.json"))
        added = record_observation(
            store, _obs("bbbbbbbb-1", "failed", {"coder": "n:llama"}, error="model timeout")
        )
        assert added and "failed" in added[0] and "model timeout" in added[0]
        assert [b.topic for b in store.bullets] == ["failure"]

    def test_repeated_outcome_folds_no_duplicates(self, tmp_path) -> None:
        store = ExperienceStore(str(tmp_path / "exp.json"))
        record_observation(store, _obs("cccccccc-1", "done", {"coder": "n:llama"}))
        record_observation(store, _obs("cccccccc-1", "done", {"coder": "n:llama"}))
        texts = [b.text for b in store.topic("success")]
        assert len(texts) == 1

    def test_long_error_is_trimmed(self, tmp_path) -> None:
        store = ExperienceStore(str(tmp_path / "exp.json"))
        added = record_observation(
            store, _obs("dddddddd-1", "failed", {"coder": "n:llama"}, error="x" * 500)
        )
        assert len(added[0]) < 300

    def test_persistence_roundtrip(self, tmp_path) -> None:
        path = os.path.join(tmp_path, "exp.json")
        store = ExperienceStore(path)
        record_observation(store, _obs("eeeeeeee-1", "done", {"planner": "n:a"}))
        store.save()
        loaded = ExperienceStore(path).load()
        assert [b.text for b in loaded.topic("success")] == [
            b.text for b in store.topic("success")
        ]


class TestFormatBullets:
    def test_newest_first_and_topic_filter(self, tmp_path) -> None:
        store = ExperienceStore(str(tmp_path / "exp.json"))
        record_observation(store, _obs("ffffffff-1", "done", {"coder": "n:a"}))
        record_observation(store, _obs("11111111-1", "failed", {"coder": "n:a"}, error="boom"))
        out = format_bullets(store, None, 10)
        lines = out.splitlines()
        assert len(lines) == 2
        assert "11111111" in lines[0]  # newest first
        assert len(format_bullets(store, "success", 10).splitlines()) == 1
        assert "failed" in format_bullets(store, "failure", 10)

    def test_limit_and_empty(self, tmp_path) -> None:
        store = ExperienceStore(str(tmp_path / "exp.json"))
        record_observation(store, _obs("22222222-1", "done", {"coder": "n:a"}))
        assert format_bullets(store, None, 0) == ""
        assert format_bullets(store, "nope", 10) == "no experience bullets"


class TestOrchestratorWiring:
    def _orch(self, settings: Settings, bus) -> Orchestrator:
        orch = Orchestrator(settings)
        orch.bus = bus  # type: ignore[assignment]
        return orch

    def _snapshot(self) -> TaskSnapshot:
        snap = TaskSnapshot(
            task_id="aaaaaaaa-1",
            trace_id="tr-1",
            task="write a function",
            state=TaskState.DONE,
            route={"coder": "n:llama"},
        )
        return snap

    def test_flag_off_records_nothing(self, tmp_path) -> None:
        path = os.path.join(tmp_path, "exp.json")
        # Explicit False: Settings is a pydantic BaseSettings reading .env, so
        # a deployment .env with EXPERIENCE_FROM_OBSERVATIONS=1 must not leak
        # into this test (init kwargs outrank env values).
        orch = self._orch(
            Settings(experience_store_path=path, experience_from_observations=False),
            _RecordingBus(),
        )
        asyncio.run(orch._deposit_observation(self._snapshot()))
        assert not os.path.exists(path)

    def test_flag_on_records_success(self, tmp_path) -> None:
        path = os.path.join(tmp_path, "exp.json")
        settings = Settings(experience_store_path=path, experience_from_observations=True)
        orch = self._orch(settings, _RecordingBus())
        asyncio.run(orch._deposit_observation(self._snapshot()))
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert any(b["topic"] == "success" for b in data)  # file is a bullet list

    def test_record_failure_is_swallowed(self, tmp_path) -> None:
        path = os.path.join(tmp_path, "no-such-dir", "exp.json")
        settings = Settings(experience_store_path=path, experience_from_observations=True)
        orch = self._orch(settings, _RecordingBus())
        # must not raise even though the store path is not writable
        asyncio.run(orch._deposit_observation(self._snapshot()))
