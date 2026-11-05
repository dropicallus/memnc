"""T7.5 groundwork: disk_models advertised in the roster."""

from memnc.schema import NodeInfo
from memnc.worker import fetch_disk_models


def test_node_info_has_disk_models_default_empty():
    node = NodeInfo(node_id="a")
    assert node.disk_models == []


def test_merge_unions_disk_models():
    a = NodeInfo(node_id="a", models=["m1"], disk_models=["m1", "m2"])
    b = NodeInfo(node_id="a", models=["m3"], disk_models=["m3"])
    merged = a.merge(b)
    assert merged.models == ["m1", "m3"]
    assert merged.disk_models == ["m1", "m2", "m3"]


def test_disk_models_snapshot_roundtrip():
    """Serialization keeps disk_models (KV roundtrip through model_dump)."""
    node = NodeInfo(node_id="a", disk_models=["x:1b", "y:7b"])
    restored = NodeInfo.model_validate(node.model_dump())
    assert restored.disk_models == ["x:1b", "y:7b"]


async def test_fetch_disk_models_best_effort_empty_on_error():
    """Unreachable Ollama yields [] instead of raising."""
    models = await fetch_disk_models("http://localhost:1")  # nothing listens here
    assert models == []
