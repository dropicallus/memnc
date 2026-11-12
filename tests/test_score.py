from unittest.mock import MagicMock

import pytest

from memnc.bus import Bus
from memnc.scoring import Candidate, score_candidates


@pytest.fixture
def mock_settings():
    settings = MagicMock()
    settings.nats_url = "nats://localhost:4222"
    settings.nats_name = "test-node"
    settings.nats_token = None
    settings.kv_bucket = "test-kv"
    settings.stream_name = "test-stream"
    return settings


@pytest.fixture
def bus(mock_settings):
    bus = Bus(mock_settings)
    from tests.test_trust_fake import FakeKV

    bus._trust = FakeKV()
    bus.js = MagicMock()
    return bus


@pytest.mark.asyncio
async def test_score_candidates():
    candidates = [
        Candidate(agent_id="a1", skill="s", capacity=10, load=0.5, latency=2.0, trust=5.0),
        Candidate(agent_id="a2", skill="s", capacity=10, load=0.1, latency=0.1, trust=9.0),
        Candidate(agent_id="a3", skill="s", capacity=20, load=0.8, latency=0.5, trust=1.0),
    ]

    scores = score_candidates(candidates)

    assert scores[0][0] == "a2"
    assert scores[0][1] >= scores[1][1] >= scores[2][1]


def test_score_favors_higher_capacity_trust():
    c1 = Candidate(agent_id="low", skill="s", capacity=5, load=0.0, latency=0.0, trust=1.0)
    c2 = Candidate(agent_id="high", skill="s", capacity=15, load=0.0, latency=0.0, trust=8.0)

    scores = score_candidates([c1, c2])
    assert scores[0][0] == "high"
