from __future__ import annotations

from pathlib import Path

from loom.runner import CaseResult, parse_junit, run_pytest

JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="4">
<testcase classname="tests.test_finish_examples" name="test_e1" file="tests/test_finish_examples.py"/>
<testcase classname="tests.test_finish_rules" name="test_r1"><failure message="assert 1 == 2">long trace</failure></testcase>
<testcase classname="tests.test_other_examples.TestX" name="test_e2"><error message="boom"/></testcase>
<testcase classname="tests.test_misc" name="test_z"><skipped message="later"/></testcase>
</testsuite></testsuites>
"""


def test_parse_junit_maps_files_and_blocks() -> None:
    cases = parse_junit(JUNIT)
    assert [(c.file, c.outcome, c.block) for c in cases] == [
        ("tests/test_finish_examples.py", "passed", "finish"),
        ("tests/test_finish_rules.py", "failed", "finish"),
        ("tests/test_other_examples.py", "error", "other"),
        ("tests/test_misc.py", "skipped", None),
    ]
    assert cases[1].message == "assert 1 == 2" and cases[1].details == "long trace"


def test_run_pytest_reports_failing_blocks(tmp_path: Path) -> None:
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "__init__.py").write_text("def answer() -> int:\n    return 41\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_answer_examples.py").write_text(
        "from pkg import answer\n\ndef test_e1():\n    assert answer() == 42\n\n"
        "def test_e2():\n    assert answer() < 100\n"
    )
    (tmp_path / "tests" / "test_other_rules.py").write_text("def test_r1():\n    assert True\n")
    result = run_pytest(tmp_path)
    assert not result.ok
    assert result.counts() == {"failed": 1, "passed": 2}
    assert result.failing_blocks() == {"answer"}
    report = result.failure_report("answer")
    assert "tests/test_answer_examples.py::test_e1 FAILED" in report and "41" in report
    assert len(result.failure_report("answer", limit=20)) <= 20 + len("\n... (truncated)")

    (tmp_path / "src" / "pkg" / "__init__.py").write_text("def answer() -> int:\n    return 42\n")
    assert run_pytest(tmp_path).ok


def test_case_result_block_only_for_generated_test_files() -> None:
    assert CaseResult("tests/test_a_b_rules.py", "t", "failed").block == "a_b"
    assert CaseResult("tests/conftest.py", "t", "error").block is None
