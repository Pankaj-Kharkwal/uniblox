"""The normalized AST hash tracks meaning, not formatting."""

from __future__ import annotations

from pathlib import Path

import pytest

from loom.hashing import does_input_hash, node_hash, shared_context_hash, source_hashes
from loom.parser import parse

ROOT = Path(__file__).resolve().parent.parent
TODO = (ROOT / "examples" / "todo.loom").read_text()
INVOICE = (ROOT / "examples" / "invoice_chaser.loom").read_text()


def hashes(src: str) -> dict[str, str]:
    return source_hashes(parse(src))


@pytest.mark.parametrize("src", [TODO, INVOICE])
def test_hash_is_stable_across_parses(src: str) -> None:
    assert hashes(src) == hashes(src)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s.replace("\n\n", "\n\n\n\n"),  # extra blank lines
        lambda s: s.replace("\n", "   \n"),  # trailing whitespace
        lambda s: s.replace("  ", "    "),  # re-indent every level from 2 to 4 spaces
        lambda s: s.replace("mark them overdue", "mark   them    overdue"),  # inner spaces
        lambda s: (
            "# header comment\n"
            + s.replace("mark them overdue", "mark them overdue  # trailing comment")
        ),
        lambda s: s.replace("  mark them overdue\n", "  # note\n  mark them overdue\n"),
        lambda s: s.replace("\n", "\r\n"),  # windows line endings
    ],
)
def test_whitespace_and_comments_do_not_change_hashes(mutate: object) -> None:
    assert callable(mutate)
    assert hashes(mutate(INVOICE)) == hashes(INVOICE)


def test_moving_blocks_does_not_change_their_hashes() -> None:
    a = parse(TODO)
    moved = TODO.replace("thing Summary", "thing Zummary")  # sanity: a rename does change it
    assert hashes(moved)["thing:Zummary"] != hashes(TODO)["thing:Summary"]
    # Swap the two things: each keeps its own hash.
    task_block = TODO[TODO.index("thing Task") : TODO.index("thing Summary")]
    swapped = TODO.replace(task_block, "").replace("exact {", task_block + "exact {")
    assert hashes(swapped)["thing:Task"] == source_hashes(a)["thing:Task"]


def test_meaningful_edits_change_only_the_edited_block() -> None:
    before = hashes(INVOICE)
    after = hashes(INVOICE.replace("mark them overdue", "mark them as overdue"))
    changed = {k for k in before if before[k] != after[k]}
    assert changed == {"does:chase_overdue_invoices"}

    before = hashes(TODO)
    after = hashes(TODO.replace("return a Summary", "return a Summary of the counts"))
    assert {k for k in before if before[k] != after[k]} == {"does:summarize_tasks"}


def test_decided_lines_change_the_hash_but_plain_comments_do_not() -> None:
    base = hashes(INVOICE)["does:chase_overdue_invoices"]
    commented = INVOICE.replace("  mark them overdue\n", "  mark them overdue\n    # just a note\n")
    decided = INVOICE.replace(
        "  mark them overdue\n", "  mark them overdue\n    # decided: only sent invoices\n"
    )
    assert hashes(commented)["does:chase_overdue_invoices"] == base
    assert hashes(decided)["does:chase_overdue_invoices"] != base


def test_exact_code_whitespace_is_significant() -> None:
    a = hashes(TODO)["exact"]
    b = hashes(TODO.replace('" ".join', '"  ".join'))["exact"]
    assert a != b


def test_input_hash_depends_on_shared_context_and_other_signatures() -> None:
    p = parse(TODO)
    complete = p.does[0]
    base = does_input_hash(p, complete, {"summarize_tasks": "def summarize_tasks() -> X"})
    assert base == does_input_hash(p, complete, {"summarize_tasks": "def summarize_tasks() -> X"})
    assert base != does_input_hash(p, complete, {"summarize_tasks": "def summarize_tasks() -> Y"})

    p2 = parse(TODO.replace("  overdue: number = 0", "  overdue: number = 0\n  late: number = 0"))
    assert shared_context_hash(p2) != shared_context_hash(p)
    assert node_hash(p2.does[0]) == node_hash(complete)  # the block itself is unchanged
    assert does_input_hash(p2, p2.does[0], {}) != does_input_hash(p, complete, {})
