import pytest
from pydantic import ValidationError

from memnc.schema import MAX_TASK_CHARS, TaskSubmission


def test_task_length_is_bounded():
    with pytest.raises(ValidationError):
        TaskSubmission(task="x" * (MAX_TASK_CHARS + 1))


def test_depth_and_revisions_are_bounded():
    with pytest.raises(ValidationError):
        TaskSubmission(task="t", depth=-1)
    with pytest.raises(ValidationError):
        TaskSubmission(task="t", max_revisions=999)
    assert TaskSubmission(task="t", max_revisions=2).max_revisions == 2
