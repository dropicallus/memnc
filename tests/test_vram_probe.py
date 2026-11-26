"""VRAM live-probe: real free/total VRAM per node + loaded model list.

Root cause of the VRAM-blockade chain (#132): NodeInfo.vram_gb was static
(.env), never measured. A loaded 14.4 GB critic therefore looked "free" to
the planner. These tests pin the probe: sysfs primary (amdgpu), rocm-smi
fallback, /api/ps for loaded models, and NodeInfo carrying live numbers.
"""

import json
import subprocess  # noqa: S404 - rocm-smi fallback is invoked with a fixed argv
from pathlib import Path

import pytest

from memnc.schema import NodeInfo
from memnc.vram_probe import (
    fetch_loaded_models,
    probe_gpus,
    probe_vram,
    vram_summary,
)

# --- sysfs primary ----------------------------------------------------------


def _make_card(root: Path, idx: int, used: int, total: int) -> None:
    dev = root / f"card{idx}" / "device"
    dev.mkdir(parents=True)
    (dev / "mem_info_vram_used").write_text(f"{used}\n")
    (dev / "mem_info_vram_total").write_text(f"{total}\n")


def test_probe_sums_all_cards(tmp_path, monkeypatch):
    _make_card(tmp_path, 0, used=16_214_548_480, total=17_163_091_968)
    _make_card(tmp_path, 1, used=27_213_824, total=17_163_091_968)
    monkeypatch.setattr("memnc.vram_probe.SYSFS_DRM", tmp_path)
    used, total = probe_vram()
    assert total == 2 * 17_163_091_968
    assert used == 16_214_548_480 + 27_213_824


def test_probe_ignores_empty_or_partial_cards(tmp_path, monkeypatch):
    # card0 has no mem_info files (iGPU or pre-init) — must not crash.
    (tmp_path / "card0" / "device").mkdir(parents=True)
    _make_card(tmp_path, 1, used=1_000, total=2_000)
    monkeypatch.setattr("memnc.vram_probe.SYSFS_DRM", tmp_path)
    used, total = probe_vram()
    assert (used, total) == (1_000, 2_000)


def test_probe_no_cards_returns_zeros(tmp_path, monkeypatch):
    monkeypatch.setattr("memnc.vram_probe.SYSFS_DRM", tmp_path / "missing")
    # Kill the live-machine fallbacks so the test never touches real GPUs.
    monkeypatch.setattr("memnc.vram_probe._probe_gpus_amdgpu_top", lambda: [])
    monkeypatch.setattr("memnc.vram_probe._probe_gpus_rocm_smi", lambda: [])
    # Legacy single-tuple probes still referenced by probe_vram fallback path.
    monkeypatch.setattr("memnc.vram_probe._probe_amdgpu_top", lambda: None)
    monkeypatch.setattr("memnc.vram_probe._probe_rocm_smi", lambda: None)
    assert probe_vram() == (0, 0)


def test_vram_summary_mb():
    used, total = vram_summary(sysfs_used=8 * 2**20, sysfs_total=16 * 2**20)
    assert used == 8.0
    assert total == 16.0


def test_rocm_smi_fallback_when_sysfs_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("memnc.vram_probe.SYSFS_DRM", tmp_path / "missing")
    monkeypatch.setattr("memnc.vram_probe._probe_gpus_amdgpu_top", lambda: [])

    class FakeRun:
        def __init__(self, stdout: str) -> None:
            self.stdout = stdout

    def fake_run(args, **kwargs):
        assert args[0] == "rocm-smi"
        assert "--json" in args
        return FakeRun(
            json.dumps(
                {
                    "card0": {
                        "VRAM Total Used Memory (B)": "16214548480",
                        "VRAM Total Memory (B)": "17163091968",
                    },
                    "card1": {
                        "VRAM Total Used Memory (B)": "27213824",
                        "VRAM Total Memory (B)": "17163091968",
                    },
                }
            )
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    used, total = probe_vram()
    assert total == 2 * 17_163_091_968
    assert used == 16_214_548_480 + 27_213_824


# --- /api/ps: what is actually resident in VRAM -----------------------------


def test_fetch_loaded_models_parses_ps(monkeypatch):
    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(
                {
                    "models": [
                        {"name": "hf.co/x/Gemma-4-31B:Q4_K_M", "size_vram": 15_000_000_000},
                        {"name": "qwen2.5:7b", "size_vram": 5_000_000_000},
                        {"name": "coder:latest", "size_vram": 0},
                    ]
                }
            ).encode()

    monkeypatch.setattr(
        "memnc.vram_probe._urlopen", lambda url, timeout: FakeResp()
    )
    loaded = fetch_loaded_models("http://127.0.0.1:11434")
    # Only models actually resident (size_vram > 0) count as loaded.
    assert loaded == [
        {"model": "hf.co/x/Gemma-4-31B:Q4_K_M", "vram_mb": 14305.1},
        {"model": "qwen2.5:7b", "vram_mb": 4768.4},
    ]


def test_fetch_loaded_models_failure_is_best_effort(monkeypatch):
    def boom(url, timeout):
        raise OSError("no ollama")

    monkeypatch.setattr("memnc.vram_probe._urlopen", boom)
    assert fetch_loaded_models("http://127.0.0.1:11434") == []


# --- NodeInfo carries live numbers ------------------------------------------


def test_nodeinfo_vram_fields_and_merge():
    a = NodeInfo(
        node_id="node-a",
        vram_free_mb=1000.0,
        vram_total_mb=16384.0,
        loaded_models=["qwen2.5:7b"],
    )
    b = NodeInfo(
        node_id="node-a",
        vram_free_mb=800.0,
        vram_total_mb=16384.0,
        loaded_models=["hf.co/x/Gemma-4-31B:Q4_K_M"],
    )
    m = a.merge(b)
    # Merge must NOT lose or inflate live VRAM state.
    assert m.vram_total_mb == 16384.0
    assert m.vram_free_mb == 800.0  # conservative: min of the two probes
    assert set(m.loaded_models) == {"qwen2.5:7b", "hf.co/x/Gemma-4-31B:Q4_K_M"}


def test_nodeinfo_zero_vram_merge_keeps_nonzero():
    a = NodeInfo(node_id="node-a", vram_free_mb=0.0, vram_total_mb=0.0)
    b = NodeInfo(node_id="node-a", vram_free_mb=500.0, vram_total_mb=16384.0)
    m = a.merge(b)
    assert m.vram_free_mb == 500.0
    assert m.vram_total_mb == 16384.0


@pytest.mark.parametrize(
    "kv",
    [
        {"vram_free_mb": 1.5, "vram_total_mb": 2.0},  # KV-safe floats round-trip
    ],
)
def test_nodeinfo_kv_roundtrip(kv):
    n = NodeInfo(node_id="n", **kv)
    assert NodeInfo.model_validate(n.model_dump()).vram_free_mb == kv["vram_free_mb"]


def _patch_rocm_dead(monkeypatch):
    # rocm-smi must not fire during amdgpu_top tests: make it return None.
    monkeypatch.setattr("memnc.vram_probe._probe_gpus_rocm_smi", lambda: [])
    monkeypatch.setattr("memnc.vram_probe._probe_rocm_smi", lambda: None)


def test_amdgpu_top_fallback_when_sysfs_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("memnc.vram_probe.SYSFS_DRM", tmp_path / "missing")
    _patch_rocm_dead(monkeypatch)

    class FakeRun:
        def __init__(self, stdout: str) -> None:
            self.stdout = stdout

    def fake_run(args, **kwargs):
        assert args[0] == "amdgpu_top"
        return FakeRun(
            json.dumps(
                {
                    "vram": {
                        "vram_used": "15461",  # MiB strings in real output
                        "total_vram": "16368",
                    }
                }
            )
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    used, total = probe_vram()
    assert total == 16368 * 2**20
    assert used == 15461 * 2**20


def test_probe_order_sysfs_wins_over_amdgpu_top(tmp_path, monkeypatch):
    _make_card(tmp_path, 0, used=100, total=200)
    monkeypatch.setattr("memnc.vram_probe.SYSFS_DRM", tmp_path)
    monkeypatch.setattr(
        "memnc.vram_probe._probe_amdgpu_top",
        lambda: (_ for _ in ()).throw(AssertionError("amdgpu_top must not run")),
    )
    assert probe_vram() == (100, 200)


# --- per-GPU probe: summing masks a full card -------------------------------


def test_probe_gpus_lists_each_card(tmp_path, monkeypatch):
    _make_card(tmp_path, 0, used=16_214_548_480, total=17_163_091_968)
    _make_card(tmp_path, 1, used=27_213_824, total=17_163_091_968)
    monkeypatch.setattr("memnc.vram_probe.SYSFS_DRM", tmp_path)
    gpus = probe_gpus()
    assert gpus == [
        (16_214_548_480, 17_163_091_968),
        (27_213_824, 17_163_091_968),
    ]


def test_health_headline_is_worst_card_not_sum(tmp_path, monkeypatch):
    # Real bug: summing card0 (nearly full) + card1 (empty) said "17 GB free"
    # while Ollama's card was 100% full. Headline must be the worst card.
    _make_card(tmp_path, 0, used=16_214_548_480, total=17_163_091_968)
    _make_card(tmp_path, 1, used=27_213_824, total=17_163_091_968)
    monkeypatch.setattr("memnc.vram_probe.SYSFS_DRM", tmp_path)
    from memnc.model_manager import ModelManager

    health = ModelManager("n", "http://127.0.0.1:11434").collect_health()
    assert health.gpu.vram_free_mb == pytest.approx(
        (17_163_091_968 - 16_214_548_480) / 2**20, rel=1e-3
    )
    assert len(health.gpu.cards) == 2
    assert health.gpu.cards[0].vram_free_mb < health.gpu.cards[1].vram_free_mb
