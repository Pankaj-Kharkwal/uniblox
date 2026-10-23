from __future__ import annotations

import ast

import pytest

from loom.validate import check_example_tests, check_module, check_rule_tests, function_info

GOOD = """
from datetime import datetime
from app.models import Task
from app.runtime import guard, utcnow


def finish(tasks: list[Task], title: str, *, now: datetime | None = None) -> Task:
    guard(True, "finish:R1")
    return tasks[0]
"""


def errors_of(source: str, rules: tuple[str, ...] = ("finish:R1",)) -> list[str]:
    return check_module(source, slug="finish", rule_ids=rules, package="app")[1]


def test_good_module_passes_and_signature_is_extracted() -> None:
    info, errors = check_module(GOOD, slug="finish", rule_ids=["finish:R1"], package="app")
    assert errors == []
    assert info is not None
    assert info.signature == (
        "def finish(tasks: list[Task], title: str, *, now: datetime | None=None) -> Task"
    )
    assert info.required == ("tasks", "title") and info.kwonly == ("now",)


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("def finish(:\n", "not valid Python"),
        (
            GOOD.replace("def finish", "def other"),
            "must define a top-level function named 'finish'",
        ),
        (GOOD.replace('guard(True, "finish:R1")', "pass"), "rule finish:R1 has no runtime guard"),
        ("import os\n" + GOOD, "import of 'os' is not allowed"),
        ("import loom.runtime\n" + GOOD, "import of 'loom.runtime' is not allowed"),
        ("import requests\n" + GOOD, "not available in the generated project"),
        ("from . import x\n" + GOOD, "use absolute imports"),
        ("# built by Loom\n" + GOOD, "must not mention Loom"),
        (GOOD + "\nopen('x')\n", "must not call open()"),
    ],
)
def test_bad_modules_are_rejected(source: str, message: str) -> None:
    assert any(message in e for e in errors_of(source)), errors_of(source)


def test_guard_found_by_keyword_or_attribute() -> None:
    src = GOOD.replace('guard(True, "finish:R1")', 'runtime.guard(True, rule_id="finish:R1")')
    assert errors_of(src) == []


def test_function_info_handles_defaults_and_missing() -> None:
    tree = ast.parse("def f(a, b=1, *, c, d=2) -> None: ...")
    info = function_info(tree, "f")
    assert info is not None and info.required == ("a", "c")
    assert function_info(tree, "g") is None


EXAMPLES = """
import pytest
from app.finish import finish
from app.runtime import RuleViolation


def test_e1_x(now):
    t = finish([1], "a", now=now)
    assert t == 1
    assert t != 2


def test_e2_y(now):
    with pytest.raises(RuleViolation):
        finish([], "a", now=now)


def test_e3_no_call():
    assert True
"""


def ex_errors(
    mapping: list[dict[str, object]], ids: dict[str, list[str]] | None = None
) -> list[str]:
    return check_example_tests(
        EXAMPLES,
        slug="finish",
        package="app",
        examples=ids or {"E1": ["E1.1", "E1.2"], "E2": ["E2.1"]},
        mapping=mapping,
    )


GOOD_MAPPING: list[dict[str, object]] = [
    {
        "example": "E1",
        "function": "test_e1_x",
        "clauses": [
            {"clause": "E1.1", "check": "assert t == 1"},
            {"clause": "E1.2", "check": "assert   t != 2  "},
        ],
    },
    {
        "example": "E2",
        "function": "test_e2_y",
        "clauses": [{"clause": "E2.1", "check": "with pytest.raises(RuleViolation):"}],
    },
]


def test_example_tests_accepted_when_every_clause_is_checked() -> None:
    assert ex_errors(GOOD_MAPPING) == []


@pytest.mark.parametrize(
    ("mapping", "message"),
    [
        (GOOD_MAPPING[:1], "example E2 has no test"),
        ([{**GOOD_MAPPING[0], "function": "test_nope"}, GOOD_MAPPING[1]], "is not in the module"),
        (
            [{**GOOD_MAPPING[0], "clauses": GOOD_MAPPING[0]["clauses"][:1]}, GOOD_MAPPING[1]],  # type: ignore[index]
            "expect clause E1.2 has no assert",
        ),
        (
            [
                {
                    **GOOD_MAPPING[0],
                    "clauses": [
                        {"clause": "E1.1", "check": "assert t == 1"},
                        {"clause": "E1.2", "check": "assert t == 3"},
                    ],
                },
                GOOD_MAPPING[1],
            ],
            "expect clause E1.2: the reported check is not an assert",
        ),
        (
            [
                {
                    **GOOD_MAPPING[0],
                    "clauses": [
                        {"clause": "E1.1", "check": "t == 1"},
                        {"clause": "E1.2", "check": "assert t != 2"},
                    ],
                },
                GOOD_MAPPING[1],
            ],
            "expect clause E1.1: the reported check is not an assert",
        ),
    ],
)
def test_example_tests_rejected(mapping: list[dict[str, object]], message: str) -> None:
    assert any(message in e for e in ex_errors(mapping)), ex_errors(mapping)


def test_example_test_must_call_the_function() -> None:
    errs = ex_errors(
        [
            {
                "example": "E3",
                "function": "test_e3_no_call",
                "clauses": [{"clause": "E3.1", "check": "assert True"}],
            }
        ],
        {"E3": ["E3.1"]},
    )
    assert errs == ["example E3: test_e3_no_call never calls finish()"]


def test_rule_tests() -> None:
    mapping = [
        {"rule": "R1", "function": "test_e1_x"},
        {"rule": "R2", "function": "test_e3_no_call"},
    ]
    errs = check_rule_tests(
        EXAMPLES, slug="finish", package="app", rule_ids=["R1", "R2", "R3"], mapping=mapping
    )
    assert "rule R2: test_e3_no_call never calls finish()" in errs
    assert "rule R3 has no test" in errs
    assert not any(e.startswith("rule R1") for e in errs)
    no_assert = "from app.finish import finish\n\ndef test_r1(now):\n    finish([], 'a')\n"
    assert check_rule_tests(
        no_assert,
        slug="finish",
        package="app",
        rule_ids=["R1"],
        mapping=[{"rule": "R1", "function": "test_r1"}],
    ) == ["rule R1: test_r1 has no assert"]
    assert check_rule_tests("def (", slug="f", package="app", rule_ids=[], mapping=[])[
        0
    ].startswith("the rule test module is not valid Python")
