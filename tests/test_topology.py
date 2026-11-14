"""Topology support: seed-list NATS_URL + KV replica counts."""

from memnc.config import Settings


def test_nats_servers_single_url() -> None:
    s = Settings(nats_url="nats://127.0.0.1:4222")
    assert s.nats_servers == ["nats://127.0.0.1:4222"]


def test_nats_servers_seed_list() -> None:
    # comma-separated seed list -> client-side server pool (round robin /
    # reconnect across masters). Whitespace tolerated.
    s = Settings(nats_url="nats://a:4222, nats://b:4222,nats://c:4222")
    assert s.nats_servers == ["nats://a:4222", "nats://b:4222", "nats://c:4222"]


def test_nats_servers_drops_empty_segments() -> None:
    s = Settings(nats_url="nats://a:4222,,nats://b:4222,")
    assert s.nats_servers == ["nats://a:4222", "nats://b:4222"]


def test_kv_replicas_default_solo() -> None:
    assert Settings().nats_kv_replicas == 1


def test_kv_replicas_mesh_topology() -> None:
    s = Settings(nats_kv_replicas=3)
    assert s.nats_kv_replicas == 3
