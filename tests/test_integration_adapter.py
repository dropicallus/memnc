"""Integration test for AgentAdapter (M7)."""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import nats
import pytest

from memnc.adapter import AgentAdapter
from memnc.bus import Bus
from memnc.config import Settings
from memnc.schema import Capability

NATS_URL = os.environ.get("MEMNC_TEST_NATS")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not NATS_URL, reason="MEMNC_TEST_NATS not set"),
]

@pytest.mark.asyncio
async def test_agent_adapter_lifecycle():
    # 1. Setup constants and ID
    suffix = uuid.uuid4().hex[:8]
    # Use a clean agent_id with no dots to match schema/adapter rules
    agent_id = f"test-agent-{suffix}"
    skill = "code"
    node = f"test-node-{suffix}"
    
    # Construct subjects as defined in adapter.py
    work_subject = f"mem.agent.{agent_id}.work"
    
    # 2. Initialize Settings
    settings = Settings(
        nats_url=NATS_URL,
        node_name=node,
        stream_name=f"TEST_STREAM_{suffix}",
        kv_bucket=f"TEST_KV_{suffix}",
        mcp_enabled=False,
    )
    
    adapter = AgentAdapter(
        agent_id=agent_id,
        node=node,
        skills=[skill],
        settings=settings,
    )
    
    client = None
    bus = None
    adapter_task = None
    
    try:
        # --- Step 1 & 2: Start adapter and send work request ---
        # We run start() in a background task because it's an infinite loop
        adapter_task = asyncio.create_task(adapter.start())
        await asyncio.sleep(1.0) # Wait for connection/subscription
        
        client = await nats.connect(NATS_URL)
        payload = {"foo": "bar"}
        
        # Send request to the agent's specific work subject
        response = await client.request(
            work_subject,
            json.dumps(payload).encode(),
            timeout=5.0,
        )
        
        resp_data = json.loads(response.data.decode())
        assert resp_data["ok"] is True
        assert resp_data["echo"] == payload

        # --- Step 3: Verify Registry ---
        bus = Bus(settings)
        await bus.connect()
        capabilities = await bus.list_capabilities()
        
        # Verify key existence
        expected_key = Capability(agent_id=agent_id, skill=skill).key()
        assert any(cap.key() == expected_key for cap in capabilities), \
            f"Capability key {expected_key} not found in registry"

    finally:
        # --- Step 4: Teardown ---
        if client:
            await client.close()
        if adapter:
            await adapter.stop()
            # Wait for loop to exit
            try:
                if adapter_task:
                    await asyncio.wait_for(adapter_task, timeout=3.0)
            except TimeoutError:
                adapter_task.cancel()
        
        if bus and bus.nc:
            await bus.close()
