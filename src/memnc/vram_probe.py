"""T2: VRAM live-probe — real free/total VRAM per node, never registry JSON.

Order of preference (all-AMD fleet):
1. sysfs /sys/class/drm/cardN/device/mem_info_vram_{used,total} (works on
   AMD dGPUs and usually on iGPUs like the 780M),
2. amdgpu_top --json -n (iGPU/pre-init cards where sysfs files are absent),
3. rocm-smi (last resort).
Loaded-model truth comes from the serving runtime's ps endpoint (Ollama
/api/ps); only models with size_vram > 0 are really resident.
"""

from __future__ import annotations

import json
import subprocess  # noqa: S404 - fixed-argv fallback probes only
from pathlib import Path

SYSFS_DRM = Path("/sys/class/drm")
VRAM_TOTAL_FILE = "mem_info_vram_total"
VRAM_USED_FILE = "mem_info_vram_used"


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _probe_sysfs() -> tuple[int, int] | None:
    used = total = 0
    seen = False
    for card in sorted(Path(SYSFS_DRM).glob("card*")):
        dev = card / "device"
        u = _read_int(dev / VRAM_USED_FILE)
        t = _read_int(dev / VRAM_TOTAL_FILE)
        if u is None or t is None:
            continue  # iGPU pre-init or no VRAM files: not a crash
        seen = True
        used += u
        total += t
    return (used, total) if seen else None


def _probe_amdgpu_top() -> tuple[int, int] | None:
    try:
        out = subprocess.run(  # noqa: S603
            ["amdgpu_top", "--json", "-n", "-i", "0"],
            capture_output=True,
            timeout=10,
            check=True,
        ).stdout
        data = json.loads(out)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    used = total = 0
    seen = False
    # amdgpu_top JSON: list of GPUs with vram.vram_used/total (MiB or bytes).
    for gpu in data if isinstance(data, list) else [data]:
        vram = gpu.get("vram") or {}
        u, t = vram.get("vram_used"), vram.get("total_vram")
        if u is None or t is None:
            continue
        seen = True
        used += int(u) * 2**20  # MiB -> bytes
        total += int(t) * 2**20
    return (used, total) if seen else None


def _probe_rocm_smi() -> tuple[int, int] | None:
    try:
        out = subprocess.run(  # noqa: S603
            ["rocm-smi", "--showmeminfo", "vram", "--json"],
            capture_output=True,
            timeout=10,
            check=True,
        ).stdout
        data = json.loads(out)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    used = total = 0
    seen = False
    for vals in data.values():
        if not isinstance(vals, dict):
            continue
        u = vals.get("VRAM Total Used Memory (B)")
        t = vals.get("VRAM Total Memory (B)")
        if u is None or t is None:
            continue
        seen = True
        used += int(u)
        total += int(t)
    return (used, total) if seen else None


def probe_vram() -> tuple[int, int]:
    """(used_bytes, total_bytes) summed over all GPUs; (0, 0) if nothing works.

    NOTE: summing masks a full card on multi-GPU nodes. Callers that gate
    placement must use probe_gpus() and look at the worst card.
    """
    gpus = probe_gpus()
    if not gpus:
        return (0, 0)
    return (sum(u for u, _ in gpus), sum(t for _, t in gpus))


def probe_gpus() -> list[tuple[int, int]]:
    """Per-GPU (used_bytes, total_bytes), one tuple per card; [] if nothing works."""
    for probe in (_probe_gpus_sysfs, _probe_gpus_amdgpu_top, _probe_gpus_rocm_smi):
        got = probe()
        if got:
            return got
    return []


def _probe_gpus_sysfs() -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for card in sorted(Path(SYSFS_DRM).glob("card*")):
        dev = card / "device"
        u = _read_int(dev / VRAM_USED_FILE)
        t = _read_int(dev / VRAM_TOTAL_FILE)
        if u is not None and t is not None and t > 0:
            out.append((u, t))
    return out


def _probe_gpus_amdgpu_top() -> list[tuple[int, int]]:
    try:
        out = subprocess.run(  # noqa: S603
            ["amdgpu_top", "--json", "-n", "-i", "0"],
            capture_output=True,
            timeout=10,
            check=True,
        ).stdout
        data = json.loads(out)
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    result: list[tuple[int, int]] = []
    for gpu in data if isinstance(data, list) else [data]:
        vram = gpu.get("vram") or {}
        u, t = vram.get("vram_used"), vram.get("total_vram")
        if u is None or t is None:
            continue
        result.append((int(u) * 2**20, int(t) * 2**20))  # MiB -> bytes
    return result


def _probe_gpus_rocm_smi() -> list[tuple[int, int]]:
    try:
        out = subprocess.run(  # noqa: S603
            ["rocm-smi", "--showmeminfo", "vram", "--json"],
            capture_output=True,
            timeout=10,
            check=True,
        ).stdout
        data = json.loads(out)
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    result: list[tuple[int, int]] = []
    for vals in data.values():
        if not isinstance(vals, dict):
            continue
        u = vals.get("VRAM Total Used Memory (B)")
        t = vals.get("VRAM Total Memory (B)")
        if u is None or t is None:
            continue
        result.append((int(u), int(t)))
    return result


def vram_summary(
    sysfs_used: float = 0.0, sysfs_total: float = 0.0
) -> tuple[float, float]:
    """Bytes -> MB (as the twin reports them)."""
    return sysfs_used / 2**20, sysfs_total / 2**20


def _urlopen(url: str, timeout: float):
    import urllib.request

    return urllib.request.urlopen(url, timeout=timeout)  # noqa: S310


def fetch_loaded_models(base_url: str) -> list[dict]:
    """Best-effort: which models are really resident in VRAM right now.

    Works against the Ollama-compatible /api/ps. Returns
    [{"model": name, "vram_mb": float}, ...] — only entries with
    size_vram > 0. Never raises.
    """
    try:
        with _urlopen(f"{base_url.rstrip('/')}/api/ps", 5) as resp:
            data = json.loads(resp.read())
    except Exception:  # noqa: BLE001 - best effort by contract
        return []
    loaded = []
    for m in data.get("models", []):
        if m.get("size_vram", 0) > 0:
            loaded.append(
                {"model": m["name"], "vram_mb": round(m["size_vram"] / 2**20, 1)}
            )
    return loaded
