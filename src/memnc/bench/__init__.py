"""memnc.bench — verified benchmark task set (roadmap 14.15.9).

Task definitions: memnc.bench.tasks. Deterministic checkers:
memnc.bench.checkers. Reference solutions: memnc.bench.solutions.
"""

from __future__ import annotations

from pathlib import Path

from memnc.bench.tasks import BenchTask, get_task, load_tasks

__all__ = ["BenchTask", "get_task", "load_tasks", "verify_task", "write_reference"]


def verify_task(task: BenchTask, workdir: str | Path) -> list[str]:
    """Run the deterministic checker for a task; empty list = pass."""
    from memnc.bench import checkers

    return checkers._run(task.checker, Path(workdir))


def write_reference(workdir: str | Path, task_id: str) -> list[Path]:
    """Materialize the reference solution for a task into workdir.

    Writes a small shim module per produced file that imports the builder
    from memnc.bench.solutions and binds the public names (see
    solutions.SOL_NAMES). The shim is what an agent-run would leave in the
    workspace, so the checker exercises the exact same load path.
    """
    from memnc.bench import solutions

    task = get_task(task_id)
    builder_name = "sol_" + task_id.split("_", 1)[1]
    builder = getattr(solutions, builder_name, None)
    if builder is None:
        raise KeyError(f"no reference solution for {task_id}")
    names = solutions.SOL_NAMES[task_id]
    if len(names) == 1:
        bind = f"{names[0]} = _build()"
    else:
        bind = ", ".join(names) + " = _build()"
    shim = (
        f'"""Reference solution for {task_id}."""\n'
        "from memnc.bench.solutions import " + builder_name + " as _build\n\n"
        + bind + "\n"
    )
    out: list[Path] = []
    for rel in task.files:
        target = Path(workdir) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(shim, encoding="utf-8")
        out.append(target)
    return out