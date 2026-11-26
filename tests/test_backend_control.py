"""Unit tests for memnc.control — the backend lifecycle abstraction.

No live backend and no subprocess: adapters are exercised through their pure
mapping logic plus a fake adapter for registry behaviour. Live end-to-end
checks are the manual smoke test (`meshctl backend list`).
"""

from memnc.control import collect, find_control
from memnc.control.base import CLOUD, HEALTH_OK, RUNNING, Service
from memnc.control.docker import DockerControl, _endpoint, _parse_labels
from memnc.control.llamacpp import parse_cmdline, unit_from_cgroup
from memnc.control.llauncher import LlauncherControl
from memnc.control.ollama import OllamaControl


class FakeAdapter:
    name = "fake"

    def __init__(self, services):
        self._services = services
        self.started: list[str] = []

    def services(self):
        return list(self._services)

    def start(self, name):
        self.started.append(name)
        return None

    def stop(self, name):
        return None

    def restart(self, name):
        return None

    def logs(self, name, lines=100):
        return ""


# -- llauncher snapshot mapping ---------------------------------------------
def test_llauncher_maps_active_unit_and_health():
    svc = LlauncherControl._to_service(
        {
            "name": "swift",
            "service": "swift",
            "manages_service": True,
            "endpoint": "127.0.0.1:8097",
            "port_open": True,
            "health": {"ok": True},
            "model": "/models/Swift-1.5-Qwen.gguf",
            "unit": {"active_state": "active"},
        }
    )
    assert (svc.state, svc.health) == (RUNNING, HEALTH_OK)
    assert svc.models == ["Swift-1.5-Qwen.gguf"]
    assert svc.service == "swift"


def test_llauncher_cloud_backend_is_unmanaged():
    svc = LlauncherControl._to_service(
        {"name": "hf", "manages_service": False, "endpoint": "router.huggingface.co:443"}
    )
    assert svc.state == CLOUD
    assert svc.managed is False


def test_llauncher_dead_port_is_down_not_healthy():
    svc = LlauncherControl._to_service(
        {
            "name": "x",
            "manages_service": True,
            "port_open": False,
            "unit": {"active_state": "failed"},
        }
    )
    assert svc.state == "failed"
    assert svc.health == "down"


# -- registry aggregation ----------------------------------------------------
def test_collect_dedupes_endpoint_and_merges_models():
    owner = FakeAdapter(
        [Service(backend="llauncher", name="ollama", endpoint="127.0.0.1:11434", state=RUNNING)]
    )
    enrich = FakeAdapter(
        [
            Service(
                backend="ollama",
                name="ollama",
                endpoint="http://127.0.0.1:11434",
                models=["a:1b"],
            )
        ]
    )
    rows = collect([owner, enrich])
    assert len(rows) == 1
    assert rows[0].backend == "llauncher"  # process owner keeps the row
    assert rows[0].models == ["a:1b"]


def test_find_control_returns_owning_adapter():
    owner = FakeAdapter([Service(backend="llauncher", name="swift", endpoint=":8097")])
    found = find_control("swift", [owner])
    assert found is not None
    assert found[0] is owner


def test_find_control_unknown_is_none():
    assert find_control("nope", [FakeAdapter([])]) is None


# -- ollama adapter without a daemon -----------------------------------------
def test_ollama_down_is_reported_as_state_not_exception(monkeypatch):
    adapter = OllamaControl()
    monkeypatch.setattr(adapter, "_get", lambda *a, **k: None)
    rows = adapter.services()
    assert len(rows) == 1
    assert rows[0].health == "down"
    assert rows[0].endpoint.endswith(":11434")


# -- docker adapter ----------------------------------------------------------
def test_docker_accepts_labelled_container():
    assert DockerControl().accepts({"Names": "x", "Image": "busybox", "Labels": "memnc.backend=1"})


def test_docker_accepts_configured_name_even_without_label():
    assert DockerControl(containers="ollama_power").accepts(
        {"Names": "ollama_power", "Image": "custom:latest"}
    )


def test_docker_accepts_known_inference_image():
    assert DockerControl().accepts({"Names": "v", "Image": "vllm/vllm-openai:latest"})


def test_docker_rejects_mesh_infra():
    """nats/workers are mesh plumbing, not backends — no label, no image match."""
    assert not DockerControl().accepts({"Names": "memnc-nats-1", "Image": "nats:2.10-alpine"})
    assert not DockerControl().accepts({"Names": "searxng", "Image": "searxng/searxng"})


def test_docker_endpoint_prefers_published_host_port():
    assert _endpoint("0.0.0.0:11434->11434/tcp, :::11434->11434/tcp") == "127.0.0.1:11434"
    assert _endpoint("") == ""


def test_docker_labels_parse_comma_string():
    labels = _parse_labels("com.docker.compose.project=ki-station,memnc.model=a:1b")
    assert labels["com.docker.compose.project"] == "ki-station"
    assert labels["memnc.model"] == "a:1b"


def test_docker_state_map_and_health(monkeypatch):
    adapter = DockerControl()
    rows = adapter._to_service(
        {
            "Names": "ollama_power",
            "Image": "ollama/ollama:latest",
            "State": "running",
            "Status": "Up 4 hours (healthy)",
            "Ports": "0.0.0.0:11434->11434/tcp",
        }
    )
    assert (rows.state, rows.health) == (RUNNING, HEALTH_OK)
    assert rows.endpoint == "127.0.0.1:11434"


# -- native llama.cpp adapter ------------------------------------------------
def test_cmdline_parses_model_port_ctx_and_draft():
    spec = parse_cmdline(
        [
            "/opt/llama.cpp/build-hip/bin/llama-server",
            "-m",
            "/models/Swift-1.5.gguf",
            "--port=8097",
            "--host",
            "0.0.0.0",
            "-c",
            "32768",
            "-md",
            "/models/dspark-Q8_0.gguf",
        ]
    )
    assert spec["model"] == "Swift-1.5.gguf"
    assert spec["endpoint"] == "127.0.0.1:8097"
    assert spec["ctx"] == "32768"
    assert spec["draft"] == "dspark-Q8_0.gguf"
    assert spec["name"] == "Swift-1.5"


def test_cmdline_defaults_without_flags():
    spec = parse_cmdline(["/bin/llama-server"])
    assert spec["endpoint"] == "127.0.0.1:8080"
    assert spec["model"] == ""


def test_unit_from_cgroup_picks_user_unit():
    assert unit_from_cgroup(
        "0::/user.slice/user-1000.slice/user@1000.service/app.slice/swift.service\n"
    ) == ("swift.service")
    assert unit_from_cgroup("0::/user.slice/session-3.scope\n") == ""


def test_discovery_matches_only_processes_whose_argv0_is_the_server(tmp_path):
    from memnc.control.llamacpp import LlamaCppControl

    def add_proc(pid: int, argv: list[str]) -> None:
        d = tmp_path / str(pid)
        d.mkdir()
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")

    add_proc(100, ["/usr/bin/llama-server", "-m", "m.gguf", "--port", "8098"])
    add_proc(200, ["vim", "llama-server"])
    add_proc(300, ["grep", "-r", "llama-server", "."])
    ctl = LlamaCppControl(proc_root=str(tmp_path))
    matched = [pid for pid in sorted(ctl._pids()) if ctl._matches(ctl._cmdline(pid))]
    assert matched == [100]
