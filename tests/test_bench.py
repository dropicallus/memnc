"""Tests for the verified bench task set (roadmap 14.15.9)."""

from memnc.bench import checkers, get_task, load_tasks, solutions, verify_task, write_reference


def _stub_solution(workdir, task):
    """Write a deliberately wrong solution file."""
    for rel in task.files:
        path = workdir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('"""stub — provides nothing the checker needs."""\n')


def test_task_set_shape():
    tasks = load_tasks()
    assert len(tasks) == 24
    by_tier = {"easy": 0, "medium": 0, "hard": 0}
    for t in tasks:
        by_tier[t.tier] += 1
    assert by_tier == {"easy": 8, "medium": 8, "hard": 8}
    ids = [t.id for t in tasks]
    assert len(set(ids)) == 24
    # Sync tier is opt-in.
    sync_tasks = load_tasks("sync")
    assert len(sync_tasks) == 4
    assert all(t.tier == "sync" and t.amendment for t in sync_tasks)
    for t in tasks:
        assert t.files, f"{t.id} must declare produced files"
        assert t.prompt.strip(), f"{t.id} must have a prompt"
        assert t.prompt.endswith("."), f"{t.id} prompt should be a full sentence"
        assert t.id in solutions.SOL_NAMES, f"{t.id} missing in SOL_NAMES"


def test_every_prompt_names_its_file():
    for t in load_tasks():
        expected = t.files[0]
        assert expected in t.prompt, f"{t.id} prompt must name {expected}"


def test_checkers_exist():
    for t in load_tasks():
        assert callable(getattr(checkers, t.checker, None)), t.checker


def _check_passes(task, tmp_path):
    write_reference(tmp_path, task.id)
    return verify_task(task, tmp_path)


def _check_rejects_stub(task, tmp_path):
    _stub_solution(tmp_path, task)
    return verify_task(task, tmp_path)


def test_reference_solutions_pass_all_checkers(tmp_path):
    failures = {}
    for t in load_tasks():
        workdir = tmp_path / t.id
        problems = _check_passes(t, workdir)
        if problems:
            failures[t.id] = problems
    assert failures == {}


def test_checkers_reject_stubs(tmp_path):
    survivors = []
    for t in load_tasks():
        workdir = tmp_path / t.id
        problems = _check_rejects_stub(t, workdir)
        if not problems:
            survivors.append(t.id)
    assert survivors == []


def test_checkers_reject_missing_files(tmp_path):
    for t in load_tasks():
        assert verify_task(t, tmp_path / "nowhere") != []


def test_missing_checker_name_is_reported(tmp_path):
    assert checkers._run("check_does_not_exist", tmp_path) == [
        "unknown checker: check_does_not_exist"
    ]


def test_get_task_roundtrip():
    assert get_task("hard_dijkstra").tier == "hard"
    try:
        get_task("no_such_task")
    except KeyError:
        return
    raise AssertionError("unknown id must raise KeyError")


def test_load_tasks_tier_filter():
    assert len(load_tasks(tier="easy")) == 8
    assert len(load_tasks(tier="medium")) == 8
    assert len(load_tasks(tier="hard")) == 8
    assert all(t.tier == "easy" for t in load_tasks(tier="easy"))


def test_write_reference_materializes_files(tmp_path):
    t = get_task("medium_roman")
    paths = write_reference(tmp_path, t.id)
    assert [p.name for p in paths] == ["sol_roman.py"]
    assert verify_task(t, tmp_path) == []