from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from loom.nodes import Does, Exact, Program, Rule, Step, Trigger, from_data, to_data
from loom.parser import parse, parse_file

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = Path(__file__).resolve().parent / "golden"
H = "app X\ntarget: python 3.12\n"


@pytest.mark.parametrize("name", ["invoice_chaser", "todo"])
def test_golden_ast(name: str) -> None:
    program = parse_file(ROOT / "examples" / f"{name}.loom")
    data = to_data(program)
    golden = GOLDEN / f"{name}.ast.json"
    if os.environ.get("LOOM_REGEN_GOLDEN") == "1":
        golden.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    assert data == json.loads(golden.read_text())


@pytest.mark.parametrize("name", ["invoice_chaser", "todo"])
def test_round_trip_through_data(name: str) -> None:
    program = parse_file(ROOT / "examples" / f"{name}.loom")
    again = from_data(json.loads(json.dumps(to_data(program))))
    assert again == program
    assert to_data(again) == to_data(program)  # positions survive too


def test_invoice_chaser_structure() -> None:
    p = parse_file(ROOT / "examples" / "invoice_chaser.loom")
    assert p.app.name == "InvoiceChaser"
    assert p.app.target.frameworks == ("fastapi",)
    assert p.app.target.database == "sqlite"
    (invoice,) = p.things
    assert [f.name for f in invoice.fields][:3] == ["client", "client_email", "amount"]
    amount = invoice.fields[2].type
    assert (amount.base, amount.arg) == ("money", "INR")
    status = invoice.fields[4].type
    assert status.options == ("draft", "sent", "paid", "overdue")
    assert invoice.fields[5].default == "0"
    assert invoice.fields[6].type.optional

    (chase,) = p.does
    assert chase.slug == "chase_overdue_invoices"
    assert chase.trigger == Trigger("weekday at 10:00 IST")
    assert [r.sentence for r in chase.rules] == [
        "never email the same client twice in 24h",
        "always skip invoices with status paid",
    ]
    soft_step = chase.steps[2]
    assert soft_step.text == "queue a reminder email to the client"
    assert soft_step.soft is not None
    assert soft_step.soft.text == "polite first, firmer after 2nd reminder"
    assert len(chase.examples) == 3
    assert chase.examples[0].expect == ("status overdue", "1 email queued", "reminders_sent 1")
    assert chase.rule_ids()[0][0] == "R1"


def test_todo_binds_examples_to_nearest_does_and_keeps_exact_verbatim() -> None:
    p = parse_file(ROOT / "examples" / "todo.loom")
    assert [d.slug for d in p.does] == ["complete_task", "summarize_tasks"]
    assert [len(d.examples) for d in p.does] == [2, 2]
    assert p.does[1].examples[1].given == "no tasks"
    (exact,) = p.exacts
    assert exact.code == (
        'def normalize_title(title: str) -> str:\n    return " ".join(title.split()).casefold()'
    )
    assert p.app.target.database is None


def test_decided_lines_indented_same_level_and_block_level() -> None:
    src = H + (
        "does chase\n"
        "  # decided: all times are IST\n"
        "  every weekday at 10:00 IST\n"
        "    # decided: weekdays are Monday to Friday\n"
        "  queue an email ~ polite\n"
        "  # decided: firmer from the 3rd reminder on\n"
        "  # decided: never cc anyone\n"
        "  never email twice in 24h\n"
        "    # decided: 24h is a rolling window\n"
    )
    (d,) = parse(src).does
    assert [x.text for x in d.decided] == ["all times are IST"]
    trig, step, rule = d.lines
    assert isinstance(trig, Trigger) and isinstance(step, Step) and isinstance(rule, Rule)
    assert [x.text for x in trig.decided] == ["weekdays are Monday to Friday"]
    assert [x.text for x in step.decided] == ["firmer from the 3rd reminder on", "never cc anyone"]
    assert [x.text for x in rule.decided] == ["24h is a rolling window"]
    assert len(d.all_decided()) == 5


def test_keywords_need_word_boundaries_and_comments_are_ignored() -> None:
    src = H + (
        "# a comment at the top level\n"
        "does greet\n"
        "  everyone gets a greeting  # trailing comment\n"
        "  # a comment line inside the block\n"
        "  nevertheless say hi\n"
        "  always be kind\n"
    )
    (d,) = parse(src).does
    assert [type(x).__name__ for x in d.lines] == ["Step", "Step", "Rule"]
    assert d.lines[0] == Step("everyone gets a greeting")
    assert d.trigger is None


def test_example_for_binds_by_name_and_inner_exact() -> None:
    src = H + (
        "does first thing\n"
        "  do a\n"
        "  exact {\n"
        "    def helper(x: dict[str, int]) -> int:\n"
        "        return {'a': 1}['a']\n"
        "  }\n"
        "does second thing\n"
        "  do b\n"
        "example for first thing\n"
        "  given x\n"
        "  expect y, z\n"
        "  expect w\n"
        "exact { CONSTANT = 3 }\n"
    )
    p = parse(src)
    first, second = p.does
    assert len(first.examples) == 1 and second.examples == ()
    assert first.examples[0].expect == ("y", "z", "w")
    assert first.examples[0].target == "first thing"
    (inner,) = first.exacts
    assert inner.code.startswith("def helper(x: dict[str, int]) -> int:")
    assert p.exacts == (Exact("CONSTANT = 3"),)


def test_windows_newlines_and_missing_final_newline() -> None:
    src = "app X\r\ntarget: python 3.12\r\ndoes x\r\n  do y"
    p = parse(src)
    assert isinstance(p, Program)
    assert isinstance(p.does[0], Does)
    assert p.does[0].steps[0].text == "do y"


def test_whitespace_inside_prose_is_collapsed() -> None:
    (d,) = parse(H + "does   chase   invoices\n  find    them   now   \n").does
    assert d.description == "chase invoices"
    assert d.steps[0].text == "find them now"
