"""End to end: `loom build` on the fixtures, replaying recorded model responses offline."""

from __future__ import annotations

import shutil
from pathlib import Path

from typer.testing import CliRunner

from loom.cli import app

ROOT = Path(__file__).resolve().parent.parent
runner = CliRunner()


def copy_example(tmp_path: Path, name: str) -> Path:
    dst = tmp_path / f"{name}.loom"
    shutil.copy(ROOT / "examples" / f"{name}.loom", dst)
    return dst


def test_build_todo_is_green_and_standalone(tmp_path: Path) -> None:
    src = copy_example(tmp_path, "todo")
    out = tmp_path / "out" / "todo_list"
    res = runner.invoke(app, ["build", str(src), "--yes", "--out", str(out)])
    assert res.exit_code == 0, res.output
    assert "GREEN: 7 passed" in res.output

    files = sorted(str(p.relative_to(out)) for p in out.rglob("*.py"))
    assert files == [
        "src/todo_list/__init__.py",
        "src/todo_list/complete_task.py",
        "src/todo_list/exact.py",
        "src/todo_list/models.py",
        "src/todo_list/outbox.py",
        "src/todo_list/runtime.py",
        "src/todo_list/summarize_tasks.py",
        "tests/__init__.py",
        "tests/conftest.py",
        "tests/test_complete_task_examples.py",
        "tests/test_complete_task_rules.py",
        "tests/test_summarize_tasks_examples.py",
        "tests/test_summarize_tasks_rules.py",
    ]
    for path in out.rglob("*.py"):
        text = path.read_text()
        assert "import loom" not in text and "from loom" not in text
    exact = (out / "src" / "todo_list" / "exact.py").read_text()
    assert 'return " ".join(title.split()).casefold()' in exact

    test = runner.invoke(app, ["test", str(src), "--out", str(out)])
    assert test.exit_code == 0, test.output
    assert "7 passed" in test.output


def test_loom_test_without_a_build(tmp_path: Path) -> None:
    src = copy_example(tmp_path, "todo")
    res = runner.invoke(app, ["test", str(src), "--out", str(tmp_path / "nowhere")])
    assert res.exit_code == 1
    assert "run `loom build" in res.output
