from memnc.discuss.common import Settings


def test_nats_token_has_no_default(monkeypatch):
    monkeypatch.delenv("NATS_TOKEN", raising=False)
    assert Settings().nats_token == ""


def test_nats_token_read_from_env(monkeypatch):
    monkeypatch.setenv("NATS_TOKEN", "from-env")
    assert Settings().nats_token == "from-env"
