"""LLM call #1: list the genuine ambiguities of each does block, then resolve them.

Answers come from the user (interactively) or from the model's suggestion (``--yes``), and are
written back into the source as ``# decided:`` lines. An ambiguity left unanswered is a
compile error.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loom.context import spec_context, system_prompt
from loom.errors import UnresolvedAmbiguityError
from loom.frontend import Frontend, load_text
from loom.llm import LLM, LLMRequest
from loom.nodes import Does
from loom.render import render_does, render_line
from loom.writeback import Answer, apply_answers

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ambiguities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "line": {"type": "integer"},
                    "question": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
                    "suggested": {"type": "integer"},
                    "why": {"type": "string"},
                },
                "required": ["line", "question", "options", "suggested", "why"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["ambiguities"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Ambiguity:
    block: str  # does slug
    line: int  # index into Does.lines, -1 for the whole block
    question: str
    options: tuple[str, ...]
    suggested: int
    why: str

    @property
    def suggestion(self) -> str:
        return self.options[self.suggested]


# Asked once per open ambiguity; returns the decision text, or None to leave it unresolved.
Resolver = Callable[[Ambiguity, Does], str | None]


def request_for(fe: Frontend, does: Does) -> LLMRequest:
    prompt = (
        "Here is the does block to check. Lines are prefixed with [L<n>]; rules also carry "
        "their id.\n\n" + render_does(does, numbered=True)
    )
    return LLMRequest(
        kind="ambiguity",
        label=f"does:{does.slug}",
        system=system_prompt("ambiguity"),
        context=spec_context(fe),
        prompt=prompt,
        schema=SCHEMA,
        effort="low",
        max_tokens=16000,
    )


def scan_block(llm: LLM, fe: Frontend, does: Does) -> list[Ambiguity]:
    raw = llm.complete(request_for(fe, does))
    out: list[Ambiguity] = []
    for item in raw.get("ambiguities", []):
        options = tuple(" ".join(str(o).split()) for o in item.get("options", []) if str(o).strip())
        if not options:
            continue
        line = int(item.get("line", -1))
        if not -1 <= line < len(does.lines):
            line = -1
        suggested = int(item.get("suggested", 0))
        out.append(
            Ambiguity(
                block=does.slug,
                line=line,
                question=" ".join(str(item.get("question", "")).split()),
                options=options,
                suggested=suggested if 0 <= suggested < len(options) else 0,
                why=" ".join(str(item.get("why", "")).split()),
            )
        )
    return out


def scan(llm: LLM, fe: Frontend, blocks: Iterable[Does]) -> list[Ambiguity]:
    out: list[Ambiguity] = []
    for does in blocks:
        out.extend(scan_block(llm, fe, does))
    return out


def resolve(
    ambiguities: list[Ambiguity],
    fe: Frontend,
    *,
    yes: bool = False,
    ask: Resolver | None = None,
) -> list[Answer]:
    """Turn ambiguities into answers. Raises if any stays unanswered."""
    answers: list[Answer] = []
    open_: list[Ambiguity] = []
    for amb in ambiguities:
        does = fe.program.does_block(amb.block)
        assert does is not None
        text = amb.suggestion if yes else (ask(amb, does) if ask else None)
        if text and text.strip():
            answers.append(Answer(block=amb.block, line=amb.line, text=text))
        else:
            open_.append(amb)
    if open_:
        listing = "\n".join(f"  - {describe(a, fe)}" for a in open_)
        raise UnresolvedAmbiguityError(
            f"{len(open_)} unresolved ambiguit{'y' if len(open_) == 1 else 'ies'}:\n{listing}\n"
            "Answer them interactively, or pass --yes to accept the suggested answers."
        )
    return answers


def describe(amb: Ambiguity, fe: Frontend) -> str:
    does = fe.program.does_block(amb.block)
    where = f"does {does.description}" if does else amb.block
    if does is not None and 0 <= amb.line < len(does.lines):
        where += f', line "{render_line(does.lines[amb.line])}"'
    return f"{where}: {amb.question} (suggested: {amb.suggestion})"


def settle(
    path: Path,
    fe: Frontend,
    llm: LLM,
    blocks: Iterable[Does],
    *,
    yes: bool = False,
    ask: Resolver | None = None,
) -> tuple[Frontend, list[Answer]]:
    """Scan ``blocks``, resolve every ambiguity, write the answers into ``path`` and reload."""
    ambiguities = scan(llm, fe, blocks)
    answers = resolve(ambiguities, fe, yes=yes, ask=ask)
    if not answers:
        return fe, []
    updated = apply_answers(fe.source, fe.program, answers)
    if updated != fe.source:
        path.write_text(updated, encoding="utf-8")
    return load_text(updated, fe.path), answers
