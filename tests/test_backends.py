import json

import httpx
import pytest

from memnc.backends.ollama import OllamaBackend
from memnc.backends.openai import OpenAIBackend


def mock_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_ollama_chat_and_tool_calls():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "read_file",
                                "arguments": {"path": "a.py"},
                            }
                        }
                    ],
                }
            },
        )

    backend = OllamaBackend("http://box:11434", client=mock_client(handler))
    result = await backend.chat(
        [{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "read_file"}}],
        model="qwen",
        num_ctx=2048,
    )
    assert captured["url"].endswith("/api/chat")
    assert captured["body"]["options"]["num_ctx"] == 2048
    assert captured["body"]["stream"] is False
    assert result.tool_calls[0].name == "read_file"
    assert result.tool_calls[0].arguments == {"path": "a.py"}


@pytest.mark.asyncio
async def test_ollama_converts_tool_messages():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"message": {"content": "ok"}})

    backend = OllamaBackend("http://box:11434", client=mock_client(handler))
    messages = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "t", "arguments": '{"a": 1}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "1", "name": "t", "content": "done"},
    ]
    await backend.chat(messages, model="m")
    sent = captured["body"]["messages"]
    assert sent[1]["tool_calls"][0]["function"]["arguments"] == {"a": 1}
    assert sent[2]["role"] == "tool"
    assert sent[2]["tool_name"] == "t"


@pytest.mark.asyncio
async def test_openai_chat_parses_tool_calls():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "thinking",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "run_shell",
                                        "arguments": '{"cmd": "ls"}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        )

    backend = OpenAIBackend(
        "http://box:8000",
        api_key="secret",
        client=mock_client(handler),
    )
    result = await backend.chat(
        [{"role": "user", "content": "hi"}], model="m", num_predict=128
    )
    assert captured["url"].endswith("/v1/chat/completions")
    assert captured["auth"] == "Bearer secret"
    assert captured["body"]["max_tokens"] == 128
    assert result.tool_calls[0].arguments == {"cmd": "ls"}


@pytest.mark.asyncio
async def test_openai_base_url_with_v1_suffix():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"data": [{"id": "m"}]})

    backend = OpenAIBackend("http://box:8000/v1", client=mock_client(handler))
    models = await backend.list_models()
    assert seen["url"] == "http://box:8000/v1/models"
    assert models == ["m"]


@pytest.mark.asyncio
async def test_retries_on_transient_status():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json={"message": {"content": "ok"}})

    backend = OllamaBackend(
        "http://box:11434", client=mock_client(handler), max_retries=2
    )
    result = await backend.chat([{"role": "user", "content": "hi"}], model="m")
    assert result.content == "ok"
    assert calls["n"] == 2
