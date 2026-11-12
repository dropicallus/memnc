import httpx
import pytest

from memnc.backends import ChatResult, FallbackBackend, parse_model_map


class FakeBackend:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    async def chat(self, messages, tools=None, model="", **options):
        self.calls += 1
        if self.fail:
            raise httpx.ConnectError("down")
        return ChatResult(content=f"ok:{model}")

    async def list_models(self):
        return ["m"]

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_fallback_switches_on_connect_error():
    fb = FallbackBackend(FakeBackend(fail=True), FakeBackend(), model_map={"big": "small"})
    result = await fb.chat([{"role": "user", "content": "hi"}], model="big")
    assert result.content == "ok:small"


@pytest.mark.asyncio
async def test_fallback_recovers_via_probe():
    primary = FakeBackend()
    fb = FallbackBackend(primary, FakeBackend(), probe_every=2)
    await fb.chat([], model="x")  # primary ok
    primary.fail = True
    r2 = await fb.chat([], model="x")  # degrades
    assert r2.content == "ok:x"
    primary.fail = False
    fb._calls = 1  # next call triggers probe
    r3 = await fb.chat([], model="x")
    assert fb._degraded is False and r3.content == "ok:x"


def test_parse_model_map():
    m = parse_model_map("a=b, c = d ,junk,noeq=")
    assert m == {"a": "b", "c": "d"}
