from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fakes import replaying_llm
from typer.testing import CliRunner

from loom.build import BuildOptions, build
from loom.cli import app
from loom.diff import diff, intent_changes
from loom.eject import eject, loom_references
from loom.errors import LockError, LoomError
from loom.frontend import load, load_text
from loom.lock import Lock, lock_path

ROOT = Path(__file__).resolve().parent.parent
runner = CliRunner()


def built(tmp_path: Path, name: str = "todo") -> tuple[Path, Path]:
    src = tmp_path / f"{name}.loom"
    shutil.copy(ROOT / "examples" / f"{name}.loom", src)
    out = tmp_path / "out"
    report = build(src, replaying_llm, BuildOptions(out_dir=out, yes=True, echo=lambda s: None))
    assert report.ok
    return src, out


def sentences(src: Path) -> list[tuple[str, str]]:
    lock = Lock.load(lock_path(src))
    assert lock is not None
    return [(c.block, c.sentence) for c in intent_changes(load(src), lock)]


def test_no_changes_means_an_empty_diff(tmp_path: Path) -> None:
    src, _ = built(tmp_path)
    report = diff(load(src), Lock.load(lock_path(src)), lock_path(src))
    assert report.empty and report.changes == [] and report.plan is not None
    assert report.plan.dirty == {}


def test_intent_diff_in_plain_english(tmp_path: Path) -> None:
    src, _ = built(tmp_path)
    text = src.read_text()
    text = text.replace("  return a Summary\n", "  return the Summary\n")
    text = text.replace(
        "  never complete a task that is already done\n",
        "  never complete a task that is already done\n  never complete a task with an empty title\n",
    )
    text = text.replace("  always leave the other tasks in the list unchanged\n", "")
    text = text.replace("~ a single pass over the list is plenty", "~ keep it simple")
    text = text.replace("expect open 0, done 0, overdue 0", "expect open 0, done 0, overdue 1")
    text = text.replace(
        "  completed_at: datetime?\n", "  completed_at: datetime?\n  notes: text?\n"
    )
    text = text.replace(
        "  priority: one of low, normal, high = normal\n", "  priority: one of low, high = low\n"
    )
    text = text.replace("  done: number = 0\n", "")
    text += (
        "\ndoes archive tasks\n  every day at 23:00 UTC\n  remove done tasks\n"
        "\nexample\n  given a done task\n  expect no tasks\n"
    )
    text = text.replace('" ".join(title.split())', '" ".join(title.lower().split())')
    text = text.replace("target: python 3.12", "target: python 3.12, sqlite")
    src.write_text(text)

    got = sentences(src)
    assert got == [
        ("app", "Changed target: python 3.12 → python 3.12, sqlite"),
        ("exact", "Changed top-level exact code"),
        (
            "thing Task",
            "Changed field Task.priority: one of low, normal, high = normal → one of low, high = low",
        ),
        ("thing Task", "Added field Task.notes: text?"),
        ("thing Summary", "Removed field Summary.done"),
        ("does complete task", "Added rule: never complete a task with an empty title"),
        ("does complete task", "Removed rule: always leave the other tasks in the list unchanged"),
        (
            "does summarize tasks",
            'Changed soft preference on "count the open and done tasks": a single pass over the list is plenty → keep it simple',
        ),
        ("does summarize tasks", 'Changed step 3: "return a Summary" → "return the Summary"'),
        (
            "does summarize tasks",
            'Changed example 2 expect: "open 0, done 0, overdue 0" → "open 0, done 0, overdue 1"',
        ),
        ("does archive tasks", "Added does block: archive tasks"),
    ]


def test_steps_schedules_decisions_and_examples(tmp_path: Path) -> None:
    lf = tmp_path / "x.loom.lock"
    base = (
        "app X\ntarget: python 3.12\n\ndoes job\n  every day\n  step one\n  step two\n"
        "\nexample\n  given a\n  expect b\n"
    )
    src = tmp_path / "x.loom"
    src.write_text(base)
    from loom.hashing import node_hash
    from loom.lock import BlockLock
    from loom.nodes import to_data

    old = load(src)
    lock = Lock(model="m", source="x.loom", out_dir="out")
    for d in old.program.does:
        lock.blocks[f"does:{d.slug}"] = BlockLock(
            kind="does", source_hash=node_hash(d), input_hash="", ast=to_data(d, positions=False)
        )
    lock.save(lf)
    changed = (
        "app X\ntarget: python 3.12\n\ndoes job\n  every week\n  step zero\n  step one\n"
        "    # decided: one means 1\n\nexample\n  given c\n  expect b\n\nexample\n  given d\n  expect e\n"
    )
    got = [c.sentence for c in intent_changes(load_text(changed), lock)]
    assert got == [
        "Changed schedule: every day → every week",
        'Added step: "step zero"',
        'Removed step: "step two"',
        "Recorded decision: one means 1",
        'Changed example 1 given: "a" → "c"',
        "Added example 2: given d; expect e",
    ]
    removed = "app X\ntarget: python 3.12\n\ndoes job\n  step one\n  step two\n"
    got = [c.sentence for c in intent_changes(load_text(removed), lock)]
    assert got == ["Removed schedule: every day", "Removed example 1: given a"]
    renamed = "app X\ntarget: python 3.12\n\ndoes other job\n  step one\n"
    got = [c.sentence for c in intent_changes(load_text(renamed), lock)]
    assert got == ["Added does block: other job", "Removed does block: job"]


def test_cli_diff_shows_intent_and_code_impact(tmp_path: Path) -> None:
    src, _ = built(tmp_path)
    same = runner.invoke(app, ["diff", str(src)])
    assert same.exit_code == 0
    assert "No intent changes since the last build" in same.output

    src.write_text(src.read_text().replace("  return a Summary\n", "  return the Summary\n"))
    res = runner.invoke(app, ["diff", str(src), "--exit-code"])
    assert res.exit_code == 1
    assert res.output.splitlines()[:9] == [
        "Intent diff",
        "  does summarize tasks",
        '    - Changed step 3: "return a Summary" → "return the Summary"',
        "",
        "Code impact",
        "  does summarize_tasks regenerates (intent changed):",
        "    src/todo_list/summarize_tasks.py",
        "    tests/test_summarize_tasks_examples.py",
        "    tests/test_summarize_tasks_rules.py",
    ]
    assert "complete_task" not in res.output


def test_cli_diff_before_any_build_and_with_hand_edits(tmp_path: Path) -> None:
    src = tmp_path / "todo.loom"
    shutil.copy(ROOT / "examples" / "todo.loom", src)
    res = runner.invoke(app, ["diff", str(src)])
    assert res.exit_code == 0
    assert "No todo.loom.lock yet" in res.output and "regenerates (first build)" in res.output

    (tmp_path / "b").mkdir()
    src2, out = built(tmp_path / "b")
    (out / "src/todo_list/complete_task.py").write_text("# mine\n")
    res = runner.invoke(app, ["diff", str(src2)])
    assert "hand-edited generated files" in res.output
    assert "src/todo_list/complete_task.py" in res.output


def test_eject_leaves_a_plain_project_that_still_passes(tmp_path: Path) -> None:
    src, out = built(tmp_path, "invoice_chaser")
    (out / ".hypothesis").mkdir(exist_ok=True)
    assert loom_references(out)  # headers mention Loom before ejecting

    res = runner.invoke(app, ["eject", str(src)])
    assert res.exit_code == 1 and "pass --yes" in res.output  # never silently destructive

    res = runner.invoke(app, ["eject", str(src), "--yes"])
    assert res.exit_code == 0, res.output
    assert "No Loom references remain." in res.output
    assert "pytest: 5 passed" in res.output
    assert not lock_path(src).exists()
    assert loom_references(out) == []
    assert not (out / ".hypothesis").exists()
    for path in (out / "tests").glob("test_*.py"):
        assert path.stat().st_mode & 0o200  # writable again

    with pytest.raises(LockError, match=r"no invoice_chaser\.loom\.lock manages them"):
        build(src, replaying_llm, BuildOptions(out_dir=out, yes=True, echo=lambda s: None))


def test_eject_reports_leftovers_and_requires_a_project(tmp_path: Path) -> None:
    with pytest.raises(LoomError, match="nothing to eject"):
        eject(tmp_path / "missing", None)
    (tmp_path / "p").mkdir()
    (tmp_path / "p" / "pyproject.toml").write_text("[project]\nname = 'p'\n")
    (tmp_path / "p" / "notes.txt").write_text("built with Loom\n")
    report = eject(tmp_path / "p", None)
    assert report.leftovers == ["notes.txt"] and not report.lock_deleted
