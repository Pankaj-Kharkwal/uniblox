from __future__ import annotations

from loom.hashing import source_hashes
from loom.parser import parse
from loom.writeback import Answer, apply_answers

SRC = """app X
target: python 3.12

does chase invoices
  every weekday at 10:00 IST
  find overdue invoices  # a comment that must survive
  queue an email ~ polite
    # decided: emails go to client_email
  never email twice in 24h

does other thing
  do something
"""


def test_inserts_indented_under_the_step_and_after_existing_decisions() -> None:
    p = parse(SRC)
    out = apply_answers(
        SRC,
        p,
        [
            Answer("chase_invoices", 0, "weekdays are Monday to Friday"),
            Answer("chase_invoices", 2, "the 3rd reminder onward is firmer"),
            Answer("chase_invoices", 3, "24h   is a rolling window"),
        ],
    )
    assert out.splitlines()[4:12] == [
        "  every weekday at 10:00 IST",
        "    # decided: weekdays are Monday to Friday",
        "  find overdue invoices  # a comment that must survive",
        "  queue an email ~ polite",
        "    # decided: emails go to client_email",
        "    # decided: the 3rd reminder onward is firmer",
        "  never email twice in 24h",
        "    # decided: 24h is a rolling window",
    ]
    chase = parse(out).does[0]
    assert [d.text for d in chase.all_decided()] == [
        "weekdays are Monday to Friday",
        "emails go to client_email",
        "the 3rd reminder onward is firmer",
        "24h is a rolling window",
    ]


def test_block_level_answer_goes_first_in_the_body() -> None:
    p = parse(SRC)
    out = apply_answers(SRC, p, [Answer("chase_invoices", -1, "all times are IST")])
    assert out.splitlines()[3:5] == ["does chase invoices", "  # decided: all times are IST"]
    again = apply_answers(out, parse(out), [Answer("chase_invoices", -1, "money is in INR")])
    assert again.splitlines()[4:6] == [
        "  # decided: all times are IST",
        "  # decided: money is in INR",
    ]
    assert [d.text for d in parse(again).does[0].decided] == [
        "all times are IST",
        "money is in INR",
    ]


def test_idempotent_and_only_the_answered_block_changes_hash() -> None:
    p = parse(SRC)
    answers = [Answer("chase_invoices", 1, "only sent invoices")]
    once = apply_answers(SRC, p, answers)
    twice = apply_answers(once, parse(once), answers)
    assert once == twice
    dup = apply_answers(SRC, p, answers + answers)
    assert dup == once
    assert (
        apply_answers(SRC, p, [Answer("chase_invoices", 2, "Emails go to   CLIENT_EMAIL")]) == SRC
    )

    before, after = source_hashes(p), source_hashes(parse(once))
    assert {k for k in before if before[k] != after[k]} == {"does:chase_invoices"}


def test_blank_answers_are_ignored_and_unknown_blocks_raise() -> None:
    p = parse(SRC)
    assert apply_answers(SRC, p, [Answer("chase_invoices", 1, "   ")]) == SRC
    try:
        apply_answers(SRC, p, [Answer("nope", 0, "x")])
    except Exception as exc:
        assert "nope" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected an error")
