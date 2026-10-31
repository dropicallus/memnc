from memnc.roles import build_user_prompt
from memnc.schema import Role


def test_coder_prompt_includes_previous_failure():
    prompt = build_user_prompt(
        Role.CODER,
        {
            "task": "create fib.py",
            "last_error": "python syntax error: fib.py:12: invalid syntax",
        },
    )
    assert "PREVIOUS ATTEMPT FAILED" in prompt
    assert "fib.py:12" in prompt


def test_planner_prompt_ignores_previous_failure():
    prompt = build_user_prompt(
        Role.PLANNER, {"task": "create fib.py", "last_error": "nope"}
    )
    assert "PREVIOUS ATTEMPT FAILED" not in prompt
