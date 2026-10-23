from __future__ import annotations

from pathlib import Path

import pytest

from loom.context import spec_context
from loom.frontend import load
from loom.parser import parse
from loom.render import render_does, render_program

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("name", ["invoice_chaser", "todo"])
def test_render_round_trips_through_the_parser(name: str) -> None:
    p = parse((ROOT / "examples" / f"{name}.loom").read_text())
    assert parse(render_program(p)) == p


def test_render_round_trips_decided_and_exact() -> None:
    src = (
        "app X\ntarget: python 3.12, sqlite\n\ndoes x\n  # decided: block level\n"
        "  every day\n    # decided: at noon\n  do it ~ gently\n  exact {\n"
        "    def f() -> int:\n        return 1\n  }\n  never stop\n\nexample for x\n"
        "  given a\n  expect b, c\n"
    )
    p = parse(src)
    assert parse(render_program(p)) == p
    numbered = render_does(p.does[0], numbered=True)
    assert "[L2] exact { ... }" in numbered and "[L3] (R1) never stop" in numbered


def test_spec_context_lists_resolved_types_and_exact_helpers() -> None:
    ctx = spec_context(load(ROOT / "examples" / "todo.loom"))
    assert "priority: one of low, normal, high = normal -> TaskPriority" in ctx
    assert "completed_at: datetime?" in ctx and "Optional[datetime]" in ctx
    assert "def normalize_title" in ctx
    inv = spec_context(load(ROOT / "examples" / "invoice_chaser.loom"))
    assert "Decimal  (currency INR)" in inv
