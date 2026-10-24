"""Incremental builds: the lock decides what regenerates, and an unchanged build calls nobody."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeLLM, synthetic_llm, synthetic_outputs

from loom.build import BuildOptions, build
from loom.errors import LockError
from loom.lock import Lock

ROOT = Path(__file__).resolve().parent.parent
SUMMARIZE = "does:summarize_tasks"
COMPLETE = "does:complete_task"


def setup(tmp_path: Path) -> tuple[Path, Path]:
    src = tmp_path / "todo.loom"
    shutil.copy(ROOT / "examples" / "todo.loom", src)
    return src, tmp_path / "out"


def run(src: Path, out: Path, llm: FakeLLM | None, **kw: Any) -> Any:
    def factory() -> FakeLLM:
        if llm is None:
            raise AssertionError("no model client should be created")
        return llm

    return build(src, factory, BuildOptions(out_dir=out, yes=True, echo=lambda s: None, **kw))


def test_first_build_then_nothing_to_rebuild(tmp_path: Path) -> None:
    src, out = setup(tmp_path)
    llm = synthetic_llm()
    report = run(src, out, llm)
    assert report.ok and report.regenerated == ["complete_task", "summarize_tasks"]
    kinds = sorted(c.kind for c in llm.calls)
    assert kinds == ["ambiguity"] * 2 + ["codegen"] * 2 + ["example_tests"] * 2 + ["rule_tests"] * 2

    lock = Lock.load(tmp_path / "todo.loom.lock")
    assert lock is not None and lock.out_dir == "out"
    assert {k: b.kind for k, b in lock.blocks.items() if b.kind in ("app", "exact")} == {
        "app": "app",
        "exact": "exact",
    }
    assert {k: b.status for k, b in lock.blocks.items() if b.kind in ("thing", "does")} == {
        "thing:Task": "green",
        "thing:Summary": "green",
        COMPLETE: "green",
        SUMMARIZE: "green",
    }
    assert lock.blocks[COMPLETE].signature is not None
    assert set(lock.blocks[SUMMARIZE].tests) == {
        "tests/test_summarize_tasks_examples.py",
        "tests/test_summarize_tasks_rules.py",
    }

    again = run(src, out, None)  # the factory raises if a client is created
    assert again.nothing_to_rebuild
    assert Lock.load(tmp_path / "todo.loom.lock") == lock


def test_editing_one_does_line_regenerates_only_that_block(tmp_path: Path) -> None:
    src, out = setup(tmp_path)
    run(src, out, synthetic_llm())
    complete_py = out / "src/todo_list/complete_task.py"
    complete_before = complete_py.read_text()
    complete_tests = (out / "tests/test_complete_task_examples.py").read_text()
    lock_before = Lock.load(tmp_path / "todo.loom.lock")
    assert lock_before is not None

    src.write_text(src.read_text().replace("return a Summary\n", "return the Summary\n"))
    llm = synthetic_llm()
    report = run(src, out, llm)
    assert report.ok
    assert report.plan.dirty == {"summarize_tasks": "intent changed"}
    assert report.regenerated == ["summarize_tasks"]
    assert {c.label for c in llm.calls} == {SUMMARIZE}
    assert complete_py.read_text() == complete_before
    assert (out / "tests/test_complete_task_examples.py").read_text() == complete_tests

    lock = Lock.load(tmp_path / "todo.loom.lock")
    assert lock is not None
    assert lock.blocks[COMPLETE] == lock_before.blocks[COMPLETE]
    assert lock.blocks[SUMMARIZE].source_hash != lock_before.blocks[SUMMARIZE].source_hash
    assert run(src, out, None).nothing_to_rebuild


def test_formatting_and_comments_do_not_trigger_a_rebuild(tmp_path: Path) -> None:
    src, out = setup(tmp_path)
    run(src, out, synthetic_llm())
    src.write_text("# a new comment\n" + src.read_text().replace("\n\n", "\n\n\n"))
    assert run(src, out, None).nothing_to_rebuild


def test_changing_a_thing_regenerates_every_does_block(tmp_path: Path) -> None:
    src, out = setup(tmp_path)
    run(src, out, synthetic_llm())
    src.write_text(
        src.read_text().replace(
            "  overdue: number = 0\n", "  overdue: number = 0\n  late: number = 0\n"
        )
    )
    report = run(src, out, synthetic_llm())
    assert set(report.plan.dirty) == {"complete_task", "summarize_tasks"}
    assert "late: int = 0" in (out / "src/todo_list/models.py").read_text()


def test_hand_edits_are_refused_unless_forced(tmp_path: Path) -> None:
    src, out = setup(tmp_path)
    run(src, out, synthetic_llm())
    module = out / "src/todo_list/summarize_tasks.py"
    module.write_text(module.read_text() + "\n# tweak\n")
    with pytest.raises(LockError, match="edited by hand"):
        run(src, out, None)
    llm = synthetic_llm()
    report = run(src, out, llm, force=True)
    assert report.ok and sorted(report.regenerated) == ["complete_task", "summarize_tasks"]
    assert "# tweak" not in module.read_text()


def test_missing_generated_file_regenerates_its_block(tmp_path: Path) -> None:
    src, out = setup(tmp_path)
    run(src, out, synthetic_llm())
    (out / "src/todo_list/complete_task.py").unlink()
    report = run(src, out, synthetic_llm())
    assert report.plan.dirty == {"complete_task": "generated files are missing"}


BUGGY = synthetic_outputs()[("codegen", SUMMARIZE)]["module_source"].replace(
    "summary.done += 1", "summary.done += 2"
)


def test_red_block_is_repaired_or_reported_and_retried_next_time(tmp_path: Path) -> None:
    src, out = setup(tmp_path)
    good = synthetic_outputs()[("codegen", SUMMARIZE)]
    buggy = {**good, "module_source": BUGGY}
    still_buggy = {"module_source": BUGGY, "notes": "tried"}
    llm = synthetic_llm({("codegen", SUMMARIZE): buggy, ("repair", SUMMARIZE): still_buggy})
    report = run(src, out, llm, max_iters=2)
    assert not report.ok and report.loop.rounds == 2
    assert report.loop.iterations == {"summarize_tasks": 2}
    assert report.result.failing_blocks() == {"summarize_tasks"}
    repairs = [c for c in llm.calls if c.kind == "repair"]
    assert len(repairs) == 2 and "repair attempt 2" in repairs[1].prompt
    assert "test_e1" in repairs[0].prompt  # the failing test output is fed back

    lock = Lock.load(tmp_path / "todo.loom.lock")
    assert lock is not None
    assert lock.blocks[SUMMARIZE].status == "red" and lock.blocks[COMPLETE].status == "green"
    for rel, digest in lock.blocks[SUMMARIZE].tests.items():
        assert report.loop.test_hashes[rel] == digest

    fixed = {"module_source": good["module_source"], "notes": "fixed the done count"}
    llm2 = synthetic_llm({("codegen", SUMMARIZE): buggy, ("repair", SUMMARIZE): fixed})
    report2 = run(src, out, llm2)
    assert report2.plan.dirty == {"summarize_tasks": "red in the last build"}
    assert report2.ok and report2.loop.iterations == {"summarize_tasks": 1}
    lock2 = Lock.load(tmp_path / "todo.loom.lock")
    assert lock2 is not None and lock2.blocks[SUMMARIZE].status == "green"
    assert lock2.blocks[SUMMARIZE].iterations == 1


def test_removed_block_files_are_deleted(tmp_path: Path) -> None:
    src, out = setup(tmp_path)
    run(src, out, synthetic_llm())
    text = src.read_text()
    src.write_text(text[: text.index("does summarize tasks")])
    report = run(src, out, synthetic_llm())
    assert report.plan.removed == [SUMMARIZE]
    assert report.plan.dirty == {}  # complete_task is untouched; only shared files change
    assert not (out / "src/todo_list/summarize_tasks.py").exists()
    assert not (out / "tests/test_summarize_tasks_rules.py").exists()
    lock = json.loads((tmp_path / "todo.loom.lock").read_text())
    assert SUMMARIZE not in lock["blocks"]


POSITIONAL_GIVEN = """from datetime import datetime

from hypothesis import given
from hypothesis import strategies as st

from todo_list.models import Task
from todo_list.summarize_tasks import summarize_tasks


@given(st.lists(st.builds(Task, title=st.just("x")), max_size=3))
def test_r1_each_task_counted_once(tasks, now):
    summary = summarize_tasks(tasks, now=now)
    assert summary.open + summary.done == len(tasks)
"""


def test_tests_that_cannot_run_are_retranslated_before_they_are_locked(tmp_path: Path) -> None:
    src, out = setup(tmp_path)
    good = synthetic_outputs()[("rule_tests", SUMMARIZE)]
    broken = {
        "module_source": POSITIONAL_GIVEN,
        "tests": [{"rule": "R1", "function": "test_r1_each_task_counted_once"}],
    }
    llm = synthetic_llm({("rule_tests", SUMMARIZE): [broken, good]})
    report = run(src, out, llm)
    assert report.ok and report.loop.rounds == 0  # no repair round wasted on correct code
    rule_calls = [c for c in llm.calls if c.kind == "rule_tests" and c.label == SUMMARIZE]
    assert len(rule_calls) == 2
    assert "could not run" in rule_calls[1].prompt
    assert "fixture 'tasks' not found" in rule_calls[1].prompt
    assert str(tmp_path) not in rule_calls[1].prompt  # paths are project-relative
    assert (
        "test_r1_each_task_is_counted_exactly_once"
        in (out / "tests/test_summarize_tasks_rules.py").read_text()
    )


def test_a_test_that_stays_broken_is_reported_not_repaired(tmp_path: Path) -> None:
    src, out = setup(tmp_path)
    broken = {
        "module_source": POSITIONAL_GIVEN,
        "tests": [{"rule": "R1", "function": "test_r1_each_task_counted_once"}],
    }
    llm = synthetic_llm({("rule_tests", SUMMARIZE): broken})
    report = run(src, out, llm)
    assert not report.ok
    assert report.loop.rounds == 0 and [c for c in llm.calls if c.kind == "repair"] == []
    (case,) = report.result.failures
    assert case.broken_test and case.block == "summarize_tasks"
