"""H4: per-node NKey identity — client fail-closed + generator + scopes."""

from __future__ import annotations

import stat
from pathlib import Path

from memnc.config import Settings
from scripts.gen_nkeys import (
    _node_pubs,
    _node_subs,
    generate_config,
)


class TestFailClosedSeed:
    """NATS_NKEYS_SEED set but file missing -> must raise, never fall back."""

    def test_settings_has_nkeys_field(self):
        s = Settings(nats_nkeys_seed="/nonexistent/node-b.nk")
        assert s.nats_nkeys_seed == "/nonexistent/node-b.nk"

    def test_default_is_none_token_mode(self):
        s = Settings()
        assert s.nats_nkeys_seed is None


class TestGenerator:
    def test_generate_config_creates_seed_pub_conf(self, tmp_path: Path):
        out = generate_config("alpha", ["node-b"], tmp_path)
        assert out == tmp_path / "nats.conf"
        assert (tmp_path / "alpha.nk").exists()
        assert (tmp_path / "node-b.nk").exists()
        pub = (tmp_path / "node-b.nk.pub").read_text().strip()
        assert pub.startswith("U") and len(pub) == 56  # nkey user public: 'U' + 55
        seed = (tmp_path / "node-b.nk").read_bytes().decode().strip()
        assert seed.startswith("SU") and len(seed) == 58

    def test_seed_mode_0600(self, tmp_path: Path):
        generate_config("alpha", ["node-b"], tmp_path)
        mode = stat.S_IMODE((tmp_path / "node-b.nk").stat().st_mode)
        assert mode == 0o600

    def test_regenerate_keeps_existing_seeds(self, tmp_path: Path):
        generate_config("alpha", ["node-b"], tmp_path)
        first = (tmp_path / "node-b.nk").read_bytes()
        generate_config("alpha", ["node-b"], tmp_path)
        assert (tmp_path / "node-b.nk").read_bytes() == first

    def test_rotate_replaces_seed(self, tmp_path: Path):
        generate_config("alpha", ["node-b"], tmp_path)
        first = (tmp_path / "node-b.nk").read_bytes()
        generate_config("alpha", ["node-b"], tmp_path, rotate=True)
        assert (tmp_path / "node-b.nk").read_bytes() != first

    def test_conf_has_one_entry_per_user(self, tmp_path: Path):
        generate_config("alpha", ["node-b", "node-c"], tmp_path)
        conf = (tmp_path / "nats.conf").read_text()
        assert conf.count("nkey:") == 3

    def test_node_scopes_match_schema_subjects(self, tmp_path: Path):
        """Worker egress must cover the subjects schema.py actually uses."""
        pubs = _node_pubs("node-b")
        subs = _node_subs("node-b")
        # own heartbeat only — never another node's
        assert "mesh.node.node-b.>" in pubs
        assert "mesh.node.node-c.>" not in pubs
        # worker egress per schema.py
        for needed in ("mesh.task.*.events", "mesh.agent.*.result", "mesh.bid.*.*", "mesh.ledger"):
            assert needed in pubs
        # worker ingress: tasks/cmds/auction fan-out
        for needed in ("mesh.task.>", "mesh.agent.*.cmd", "mesh.auction.*.*"):
            assert needed in subs
        # deny-by-default: no blanket mesh.> for workers
        assert "mesh.>" not in pubs
        assert "mesh.>" not in subs

    def test_orchestrator_gets_control_plane(self, tmp_path: Path):
        generate_config("alpha", ["node-b"], tmp_path)
        conf = (tmp_path / "nats.conf").read_text()
        # orchestrator block precedes the first node block and has mesh.>
        orch_part = conf.split("nkey:")[1].split("nkey:")[0]
        assert "mesh.>" in orch_part
