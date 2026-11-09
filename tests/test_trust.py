import pytest
import asyncio
from datetime import datetime
from unittest.mock import MagicMock

from memnc.schema import (
    TrustEntry,
    TRUST_DEFAULT,
    TRUST_MAX,
    TRUST_MIN,
    TRUST_ACCRUE,
    TRUST_DECAY,
    TRUST_BUCKET,
)
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
    # Setup FakeKV in bus
    from test_trust_fake import FakeKV
    bus._trust = FakeKV()
    # Mock NATS objects so connect doesn't fail
    bus.js = MagicMock()
    return bus


@pytest.mark.asyncio
async def test_trust_credit_accumulates(bus):
    subject = "agent-1"
    
    # Initial credit logic: starts at TRUST_DEFAULT and accrues TRUST_ACCRUE
    await bus.trust_credit(subject, rating="orchestrator")
    entry = await bus.trust_entry(subject)
    assert entry.credit == TRUST_DEFAULT + TRUST_ACCRUE
    
    # Accrue second time
    await bus.trust_credit(subject, rating="orchestrator")
    entry = await bus.trust_entry(subject)
    assert entry.credit == TRUST_DEFAULT + 2 * TRUST_ACCRUE

    # Check clamping (force a very high value)
    # We simulate direct entry to test clamping logic in the method
    entry_high = TrustEntry(subject=subject, rating="admin", credit=9.9)
    # In a real scenario, trust_credit uses current + ACCRUE
    # await bus.trust_credit(subject, rating="admin") # This would lead to 10.4 if we forced it
    
    # Testing clamping via multiple accruals
    for _ in range(100):
        await bus.trust_credit(subject, rating="orchestrator")
    
    entry_clamped = await bus.trust_entry(subject)
    assert entry_clamped.credit <= TRUST_MAX


@pytest.mark.asyncio
async def test_trust_decay_multiplies(bus):
    subject = "agent-1"
    
    # Setup initial state
    await bus.trust_credit(subject, rating="orchestrator") # credit = 1.0
    # Force credit to 5.0 manually for testing decay
    from test_trust_fake import FakeKV
    await bus._trust.put(f"trust.{subject}", b'{"subject": "agent-1", "rating": "admin", "credit": 5.0, "ts": 0.0, "attribution": []}')
    
    await bus.trust_decay(subject)
    entry = await bus.trust_entry(subject)
    assert entry.credit == pytest.approx(5.0 * TRUST_DECAY)

    # Test start with DEFAULT * DECAY if no entry
    await bus.trust_decay("new-agent")
    entry_new = await bus.trust_entry("new-agent")
    assert entry_new.credit == pytest.approx(TRUST_DEFAULT * TRUST_DECAY)


@pytest.mark.asyncio
async def test_list_trust_sorted(bus):
    # Populate
    await bus.trust_credit("agent_b", rating="r")
    await bus.trust_credit("agent_a", rating="r")
    await bus.trust_credit("agent_c", rating="r")
    
    entries = await bus.list_trust()
    assert len(entries) == 3
    assert entries[0].subject == "agent_a"
    assert entries[1].subject == "agent_b"
    assert entries[2].subject == "agent_c"


def test_score_candidates():
    candidates = [
        Candidate(agent_id="a1", skill="s", capacity=10, load=0.5, latency=2.0, trust=5.0),
        Candidate(agent_id="a2", skill="s", capacity=10, load=0.1, latency=0.1, trust=9.0),
        Candidate(agent_id="a3", skill="s", capacity=20, load=0.8, latency=0.5, trust=1.0),
    ]
    
    # Test sorting and scoring
    scores = score_candidates(candidates)
    
    # a2 should be top because of high trust and low load
    assert scores[0][0] == "a2"
    # check if sorting is descending
    assert scores[0][1] >= scores[1][1] >= scores[2][1]

def test_score_favors_higher_capacity_trust():
    # Identical everything except capacity and trust
    c1 = Candidate(agent_id="low", skill="s", capacity=5, load=0.0, latency=0.0, trust=1.0)
    c2 = Candidate(agent_id="high", skill="s", capacity=15, load=0.0, latency=0.0, trust=8.0)
    
    scores = score_candidates([c1, c2])
    assert scores[0][0] == "high"
