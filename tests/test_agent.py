import pytest

from memnc.agent import AgentLimitError, extract_json, parse_role_output, run_agent
from memnc.backends import ChatResult, ToolCall
from memnc.schema import Role


class ScriptedBackend:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    async def chat(self, messages, tools=None, model="", **options):
        self.calls.append({"messages": list(messages), "tools": tools})
        if not self.results:
            raise AssertionError("backend called more often than scripted")
        return self.results.pop(0)

    async def list_models(self):
        return []

    async def close(self):
        return None


def test_extract_json_plain_and_fenced():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('```json\n{"a": 2}\n```') == {"a": 2}
    assert extract_json('Sure, here it is:\n{"a": 3}\nThanks') == {"a": 3}


def test_extract_json_rejects_garbage():
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_parse_planner_output():
    output = parse_role_output(
        Role.PLANNER,
        '{"plan": [{"step": 1, "description": "x", "files": []}]}',
    )
    assert output.plan[0].description == "x"


def test_parse_critic_output_rejects_missing_verdict():
    with pytest.raises(ValueError):
        parse_role_output(Role.CRITIC, '{"issues": []}')


@pytest.mark.asyncio
async def test_agent_executes_tool_then_returns_answer():
    backend = ScriptedBackend(
        [
            ChatResult(
                content="",
                tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": "a"})],
            ),
            ChatResult(content='{"approved": true}'),
        ]
    )
    executed = []

    async def execute(name, arguments):
        executed.append((name, arguments))
        return "file-content"

    run = await run_agent(
        backend=backend,
        model="m",
        messages=[{"role": "user", "content": "go"}],
        tools=[{"type": "function", "function": {"name": "read_file"}}],
        max_steps=4,
        execute_tool=execute,
    )
    assert executed == [("read_file", {"path": "a"})]
    assert run.content == '{"approved": true}'
    assert run.tool_steps == 1

    second_call_messages = backend.calls[1]["messages"]
    assert second_call_messages[-1]["role"] == "tool"
    assert second_call_messages[-1]["content"] == "file-content"
    assert second_call_messages[-2]["tool_calls"][0]["function"]["name"] == "read_file"


@pytest.mark.asyncio
async def test_agent_converts_tool_errors_into_messages():
    backend = ScriptedBackend(
        [
            ChatResult(
                content="",
                tool_calls=[ToolCall(id="c1", name="boom", arguments={})],
            ),
            ChatResult(content="done"),
        ]
    )

    async def execute(name, arguments):
        raise RuntimeError("kaput")

    run = await run_agent(
        backend=backend,
        model="m",
        messages=[{"role": "user", "content": "go"}],
        tools=[],
        max_steps=2,
        execute_tool=execute,
    )
    assert run.tool_steps == 1
    assert "kaput" in backend.calls[1]["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_agent_honours_step_limit():
    tool_call = ChatResult(content="", tool_calls=[ToolCall(id="c", name="t", arguments={})])
    backend = ScriptedBackend([tool_call, tool_call, tool_call])

    async def execute(name, arguments):
        return "ok"

    with pytest.raises(AgentLimitError):
        await run_agent(
            backend=backend,
            model="m",
            messages=[],
            tools=[],
            max_steps=2,
            execute_tool=execute,
        )
