"""The green loop on a tiny real project: repair, give up, and refuse any change to a test."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from loom.errors import GenerationError, TestTamperError
from loom.loop import green_loop, hash_tests

BROKEN = "def answer() -> int:\n    return 41\n"
FIXED = "def answer() -> int:\n    return 42\n"
TESTS = ["tests/test_answer_examples.py"]


def project(tmp_path: Path, module: str = BROKEN) -> Path:
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "__init__.py").write_text("")
    (tmp_path / "src" / "pkg" / "answer.py").write_text(module)
    (tmp_path / "tests").mkdir()
    (tmp_path / TESTS[0]).write_text(
        "from pkg.answer import answer\n\ndef test_e1():\n    assert answer() == 42\n"
    )
    return tmp_path


MODULES = {"answer": "src/pkg/answer.py"}


def test_repairs_only_the_failing_block_until_green(tmp_path: Path) -> None:
    root = project(tmp_path)
    calls: list[tuple[str, int]] = []

    def repair(slug: str, attempt: int, failures: str) -> str:
        calls.append((slug, attempt))
        assert "assert 41 == 42" in failures
        return FIXED

    out = green_loop(
        root, test_files=TESTS, modules=MODULES, repair=repair, max_iters=4, echo=lambda s: None
    )
    assert out.ok and out.rounds == 1 and out.iterations == {"answer": 1}
    assert calls == [("answer", 1)]
    assert (root / "src/pkg/answer.py").read_text() == FIXED


def test_gives_up_after_max_iters_with_tests_unchanged_and_read_only(tmp_path: Path) -> None:
    root = project(tmp_path)
    before = hash_tests(root, TESTS)
    out = green_loop(
        root,
        test_files=TESTS,
        modules=MODULES,
        repair=lambda slug, attempt, failures: BROKEN,
        max_iters=3,
        echo=lambda s: None,
    )
    assert not out.ok and out.rounds == 3 and out.iterations == {"answer": 3}
    assert out.result.failing_blocks() == {"answer"}
    assert hash_tests(root, TESTS) == before == out.test_hashes
    assert not os.access(root / TESTS[0], os.W_OK) or os.geteuid() == 0
    assert not (root / TESTS[0]).stat().st_mode & 0o222


def test_any_change_to_a_test_aborts_the_build(tmp_path: Path) -> None:
    root = project(tmp_path)

    def sneaky(slug: str, attempt: int, failures: str) -> str:
        test = root / TESTS[0]
        test.chmod(0o644)
        test.write_text(test.read_text().replace("== 42", "== 41"))  # "make the test pass"
        return BROKEN

    with pytest.raises(TestTamperError, match=r"test_answer_examples\.py"):
        green_loop(
            root,
            test_files=TESTS,
            modules=MODULES,
            repair=sneaky,
            max_iters=2,
            echo=lambda s: None,
        )


def test_rejected_repairs_are_recorded_and_the_loop_moves_on(tmp_path: Path) -> None:
    root = project(tmp_path)
    attempts = iter([GenerationError("signature changed"), FIXED])

    def repair(slug: str, attempt: int, failures: str) -> str:
        item = next(attempts)
        if isinstance(item, Exception):
            raise item
        return item

    out = green_loop(
        root, test_files=TESTS, modules=MODULES, repair=repair, max_iters=3, echo=lambda s: None
    )
    assert out.ok and out.rounds == 2
    assert out.repair_errors == {"answer": ["signature changed"]}


def test_failures_no_block_owns_stop_the_loop(tmp_path: Path) -> None:
    root = project(tmp_path, FIXED)
    (root / "tests" / "conftest.py").write_text("raise RuntimeError('broken conftest')\n")
    called = []
    out = green_loop(
        root,
        test_files=TESTS,
        modules=MODULES,
        repair=lambda *a: called.append(a) or FIXED,
        max_iters=3,
        echo=lambda s: None,
    )
    assert not out.ok and out.rounds == 0 and called == []
