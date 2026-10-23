"""Write ``# decided:`` lines back into .loom source, under the line they answer.

Edits are textual and minimal: only new lines are inserted, so the author's formatting and
comments survive. Inserting is idempotent: an answer already present is not written twice.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from loom.errors import LoomError
from loom.nodes import Decided, Exact, Program


@dataclass(frozen=True)
class Answer:
    block: str  # does slug
    line: int  # index into Does.lines, or -1 for the whole block
    text: str


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def apply_answers(source: str, program: Program, answers: list[Answer]) -> str:
    """Return ``source`` with one ``# decided:`` line inserted per new answer."""
    lines = source.split("\n")
    inserts: dict[int, list[str]] = defaultdict(list)  # 1-based line to insert after

    for ans in answers:
        does = program.does_block(ans.block)
        if does is None:
            raise LoomError(f"no does block named {ans.block!r}")
        text = " ".join(ans.text.split())
        if not text:
            continue
        target = does.lines[ans.line] if 0 <= ans.line < len(does.lines) else None
        existing: tuple[Decided, ...]
        if target is None or isinstance(target, Exact):
            # Block-level decision: first lines of the body, at the body's indentation.
            existing = does.decided
            anchor = existing[-1].pos.line if existing else does.pos.line
            indent = (does.lines[0].pos.column - 1) if does.lines else 2
        else:
            existing = target.decided
            anchor = existing[-1].pos.line if existing else target.pos.line
            indent = existing[0].pos.column - 1 if existing else target.pos.column - 1 + 2
        pending = inserts[anchor]
        known = {_norm(d.text) for d in existing} | {_norm(p.split(":", 1)[1]) for p in pending}
        if _norm(text) in known:
            continue
        pending.append(" " * indent + f"# decided: {text}")

    for anchor in sorted(inserts, reverse=True):
        lines[anchor:anchor] = inserts[anchor]
    return "\n".join(lines)
