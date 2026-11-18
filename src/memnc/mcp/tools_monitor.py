"""Monitoring tools for the memnc MCP server — the mesh digital twin.

No shell-outs to pgrep in agent chats: every status an operator normally
tail'ed / pgrep'ed / curl'ed is served here as a single structured call:
GPU/VRAM per card, inference lanes (ollama + llama-server), long-running
jobs (progress from checkpoint JSONLs + process liveness), mesh daemons
(nats-server, backlog-driver).
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)


def _http_json(url: str, timeout_s: float = 3.0) -> object | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None


def gpu_status() -> dict[str, object]:
    """Per-GPU used/total VRAM (read from kernel DRM interfaces)."""
    gpus: list[dict[str, object]] = []
    for card in sorted(Path("/sys/class/drm").glob("card[0-9]")):
        dev = card / "device"
        used = dev / "mem_info_vram_used"
        total = dev / "mem_info_vram_total"
        if not used.exists():
            continue
        try:
            gpus.append(
                {
                    "card": card.name,
                    "used_bytes": int(used.read_text().strip()),
                    "total_bytes": int(total.read_text().strip()),
                }
            )
        except (OSError, ValueError):
            continue
    return {"gpus": gpus}


def _llama_metrics(port: int = 8098) -> dict[str, float]:
    """llama-server /metrics counters we care about (None-safe)."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=3.0) as resp:
            text = resp.read().decode("utf-8", "replace")
    except Exception:
        return {}
    vals: dict[str, float] = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("llamacpp:") and not line.startswith("#"):
            name, _, value = line.partition(" ")
            try:
                vals[name.split()[0]] = float(value)
            except ValueError:
                continue
    return vals


TOK_S_WARN_BELOW = 30.0

# Throughput model is fully config-driven: GPU bandwidths and quant bits are
# HARDWARE DATA and never live in this repo — they come from a config file
# outside it (default ~/.config/memnc/throughput.json, path overridable via
# MEMNC_THROUGHPUT_CONFIG). Schema:
# {
#   "gpu_bandwidth": {"<lowercase lspci name substring>": GB/s, ...},
#   "quant_bits":    {"<lowercase gguf filename tag>": bits/weight, ...}
# }
# No config file => no throughput verdict (fields stay null). Generic code only.


def _throughput_config() -> dict[str, dict[str, float]]:
    """Load + validate the throughput config; {} on missing/invalid file.

    Validated against schemas/throughput.schema.json (jsonschema if installed,
    otherwise structural fallback). Invalid config => warn + no verdict, never
    a crash or a silently wrong 'ok'.
    """
    import os
    path = Path(
        os.environ.get("MEMNC_THROUGHPUT_CONFIG", "~/.config/memnc/throughput.json")
    ).expanduser()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        log.warning("throughput config unreadable at %s: %s", path, exc)
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("throughput config invalid JSON at %s: %s", path, exc)
        return {}
    errors = _validate_throughput_config(data)
    if errors:
        log.warning(
            "throughput config %s failed validation: %s", path, "; ".join(errors)
        )
        return {}
    return {
        "gpu_bandwidth": {
            str(k).lower(): float(v) for k, v in data["gpu_bandwidth"].items()
        },
        "quant_bits": {
            str(k).lower(): float(v) for k, v in data["quant_bits"].items()
        },
    }


def _validate_throughput_config(data: object) -> list[str]:
    """Validate against the bundled schema; structural fallback if jsonschema absent."""
    schema_path = Path(__file__).resolve().parent / "schemas" / "throughput.schema.json"
    try:
        import jsonschema

        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        validator = jsonschema.Draft7Validator(schema)
        return [
            f"{'/'.join(str(p) for p in err.absolute_path) or '<root>'}: {err.message}"
            for err in validator.iter_errors(data)
        ]
    except ImportError:
        pass
    except (OSError, json.JSONDecodeError):
        return ["bundled schema unreadable"]

    # Structural fallback: same rules as the schema, hand-rolled.
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["<root>: expected object"]
    extra = set(data) - {"gpu_bandwidth", "quant_bits"}
    if extra:
        errors.append(f"<root>: unexpected keys {sorted(extra)}")
    for key in ("gpu_bandwidth", "quant_bits"):
        table = data.get(key)
        if table is None:
            continue
        if not isinstance(table, dict):
            errors.append(f"{key}: expected object")
            continue
        for name, val in table.items():
            if not isinstance(val, (int, float)) or isinstance(val, bool):
                errors.append(f"{key}.{name}: not a number")
            elif not 0 < float(val) <= 10000:
                errors.append(f"{key}.{name}: out of range (0, 10000]")
    return errors


def _gpu_bandwidths(bw_table: dict[str, float]) -> list[tuple[str, float]]:
    """[(pci product name, effective GB/s)] for all amdgpu cards (bw 0.0 = unknown)."""
    out: list[tuple[str, float]] = []
    for card in sorted(Path("/sys/class/drm").glob("card[0-9]")):
        if not (card / "device/mem_info_vram_total").exists():
            continue
        slot = None
        try:
            resolved = (card / "device").resolve()
            addr = resolved.name
            if ":" in addr and "." in addr:
                slot = addr
        except OSError:
            continue
        if slot is None:
            continue
        try:
            out_proc = subprocess.run(
                ["lspci", "-s", slot], capture_output=True, text=True, timeout=5
            )
            name = out_proc.stdout.split(":", 2)[-1].strip() if out_proc.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            name = ""
        upper = name.upper()
        bw = next(
            (val for key, val in bw_table.items() if key.upper() in upper), 0.0
        )
        out.append((name, bw))
    return out


def _parse_model_file(path: str, quant_table: dict[str, float]) -> dict[str, float | None]:
    """params_B and bits-per-weight from a GGUF filename (e.g. model-7b-Q4_K_M.gguf)."""
    import re
    name = Path(path).name.lower()
    m = re.search(r"(\d+(?:\.\d+)?)b", name)
    params_b = float(m.group(1)) if m else None
    bits = next(
        (val for tag, val in sorted(quant_table.items(), key=lambda kv: -len(kv[0]))
         if tag in name),
        None,
    )
    return {"params_b": params_b, "bits_per_weight": bits}


def _expected_throughput(weights_gb: float, bandwidths: list[float]) -> float | None:
    """Best achievable decode tok/s: weights split across devices proportional to bandwidth.

    With per-device share s_i = bw_i / sum(bw), per-token time = sum(s_i / bw_i) = 1/sum(bw).
    -> expected = sum(bw) / weights_gb. (KV-cache reads and overhead ignored: ~15-25% off.)
    """
    if not bandwidths or weights_gb <= 0:
        return None
    return round(sum(bandwidths) / weights_gb, 1)


def inference_status() -> dict[str, object]:
    """Both inference lanes: ollama (:11434) and llama-server (:8098).

    llama_server carries a throughput verdict: cumulative tok/s from the
    server's own metrics (tokens_predicted / tokens_predicted_seconds).
    Warning below 30 tok/s — a lane that decodes slower is unsuited for
    rollout work (user-set threshold).
    """
    ollama_models = _http_json("http://127.0.0.1:11434/api/ps")
    llama = _http_json("http://127.0.0.1:8098/health")
    llama_models = _http_json("http://127.0.0.1:8098/v1/models")
    metrics = _llama_metrics()
    pred = metrics.get("llamacpp:tokens_predicted_total")
    secs = metrics.get("llamacpp:tokens_predicted_seconds_total")
    tok_s: float | None = None
    warn: bool | None = None
    if pred is not None and secs and secs > 0:
        tok_s = round(pred / secs, 1)
        warn = tok_s < TOK_S_WARN_BELOW

    # Throughput model: expected best-case from GPU bandwidth vs. model weight
    # bytes-per-token; efficiency <0.5 => setup unfit.
    # All hardware numbers come from the config file — repo stays generic.
    tc = _throughput_config()
    gpus = _gpu_bandwidths(tc.get("gpu_bandwidth", {}))
    bws = [bw for _, bw in gpus if bw > 0]
    model_path = ""
    for pid_dir in Path("/proc").iterdir():
        if not pid_dir.name.isdigit():
            continue
        try:
            cmd = (pid_dir / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if b"llama-server" in cmd[0] and b"8098" in (pid_dir / "cmdline").read_bytes():
            if b"-m" in cmd:
                model_path = cmd[cmd.index(b"-m") + 1].decode("utf-8", "replace")
            break
    model = _parse_model_file(model_path, tc.get("quant_bits", {})) if model_path else {}
    params_b = model.get("params_b") if isinstance(model, dict) else None
    bits = model.get("bits_per_weight") if isinstance(model, dict) else None
    expected = None
    verdict = None
    if params_b and bits and bws:
        weights_gb = params_b * bits / 8.0
        expected = _expected_throughput(weights_gb, bws)
        if expected and tok_s:
            eff = tok_s / expected
            verdict = (
                "ok" if eff >= 0.75
                else "suboptimal" if eff >= 0.5
                else "setup-unfit"
            )

    return {
        "ollama": {"up": ollama_models is not None, "models": ollama_models},
        "llama_server": {
            "up": llama == {"status": "ok"},
            "health": llama,
            "models": (llama_models or {}).get("models", []) if isinstance(llama_models, dict) else [],
            "tok_s": tok_s,
            "tok_s_warn_below": TOK_S_WARN_BELOW,
            "warning": warn,
            "gpus": [{"name": n, "bw_gbps": b} for n, b in gpus],
            "model": model_path.split("/")[-1] if model_path else None,
            "expected_tok_s": expected,
            "efficiency": round(tok_s / expected, 2) if expected and tok_s else None,
            "verdict": verdict,
        },
    }


def run_status(pattern: str, log: str | None = None, ckpt: str | None = None) -> dict[str, object]:
    """Status of a long-running job by process pattern.

    Reads progress from the checkpoint JSONL (kinds: eval/proposal) and the
    last lines of its log — no more tail loops in agent chats.
    """
    running = False
    pids: list[int] = []
    if shutil.which("pgrep"):
        try:
            out = subprocess.run(
                ["pgrep", "-f", pattern], capture_output=True, text=True, timeout=5
            )
            pids = [int(p) for p in out.stdout.split() if p.strip().isdigit()]
        except (subprocess.TimeoutExpired, ValueError):
            pass
    # pgrep -f matches its own caller's command line sometimes; filter via /proc
    pids = [p for p in pids if p != __import__("os").getpid()]
    running = bool(pids)

    result: dict[str, object] = {"running": running, "pids": pids}
    if ckpt:
        ck_path = Path(ckpt)
        if ck_path.exists():
            counts = {"eval": 0, "proposal": 0, "evals_ok": 0}
            for line in ck_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = rec.get("kind")
                if kind in counts and isinstance(counts[kind], int):
                    counts[kind] += 1
                if kind == "eval" and rec.get("success"):
                    counts["evals_ok"] += 1
            result["ckpt"] = counts
        else:
            result["ckpt"] = None
    if log:
        log_path = Path(log)
        if log_path.exists():
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            result["log_tail"] = lines[-8:]
        else:
            result["log_tail"] = None
    return result


def daemon_status(units: str = "memnc-backlog-driver") -> dict[str, object]:
    """systemd --user unit states (comma-separated list)."""
    out: dict[str, object] = {}
    for unit in units.split(","):
        try:
            proc = subprocess.run(
                ["systemctl", "--user", "is-active", unit.strip()],
                capture_output=True,
                text=True,
                timeout=5,
            )
            out[unit.strip()] = proc.stdout.strip() or proc.stderr.strip()
        except (subprocess.TimeoutExpired, OSError) as exc:
            out[unit.strip()] = f"error: {exc}"
    return out
