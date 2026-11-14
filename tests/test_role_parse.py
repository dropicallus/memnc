"""Robust role-output parsing: chatter and tool-call echoes must not break roles."""

import asyncio

from memnc.agent import parse_role_output
from memnc.schema import Role


def test_tool_call_echo_before_result_parses_result() -> None:
    text = (
        '{"name": "write_file", "arguments": {"path": "a.txt", "content": "x"}}\n'
        '{"summary": "done", "files": [{"path": "a.txt", "content": "x"}], "commands": []}'
    )
    out = parse_role_output(Role.CODER, text)
    assert [f.path for f in out.files] == ["a.txt"]
    assert out.summary == "done"


def test_richest_candidate_wins_over_empty_object() -> None:
    text = '{}\n{"summary": "created file", "files": [{"path": "f.py", "content": "1"}]}'
    out = parse_role_output(Role.CODER, text)
    assert len(out.files) == 1


def test_planner_chatter_still_parses_first_valid() -> None:
    out = parse_role_output(Role.PLANNER, '{"plan": [{"description": "write file"}]}')
    assert out.plan


def test_garbage_raises_value_error() -> None:
    try:
        parse_role_output(Role.CODER, "no json here at all")
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


def test_no_valid_object_raises_value_error() -> None:
    try:
        parse_role_output(Role.CODER, '{"name": "write_file", "arguments": {}}')
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


def test_sync_entrypoint() -> None:
    asyncio.run(_round_trip())


async def _round_trip() -> None:
    parse_role_output(Role.CRITIC, '{"approved": true, "summary": "looks good"}')
