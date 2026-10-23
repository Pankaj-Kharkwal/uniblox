"""Typed AST for Loom programs.

Every node is a frozen dataclass. ``pos`` records where the node starts in the source; it is
excluded from equality and from hashing, so moving a block around or re-indenting it does not
count as an intent change.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
from typing import Any, Literal

RuleKind = Literal["never", "always"]


@dataclass(frozen=True)
class Pos:
    line: int
    column: int


NO_POS = Pos(0, 0)


@dataclass(frozen=True)
class Target:
    """The parsed ``target:`` line, e.g. ``python 3.12, fastapi, sqlite``."""

    language: str
    version: str
    frameworks: tuple[str, ...] = ()
    database: str | None = None
    pos: Pos = field(default=NO_POS, compare=False)


@dataclass(frozen=True)
class App:
    name: str
    target: Target
    pos: Pos = field(default=NO_POS, compare=False)


@dataclass(frozen=True)
class TypeRef:
    """A forgiving type exactly as written, before resolution."""

    base: str  # "text", "money", ... or "one of"
    arg: str | None = None  # the currency in ``money (INR)``
    options: tuple[str, ...] = ()  # the choices in ``one of a, b``
    optional: bool = False
    pos: Pos = field(default=NO_POS, compare=False)


@dataclass(frozen=True)
class Field:
    name: str
    type: TypeRef
    default: str | None = None
    pos: Pos = field(default=NO_POS, compare=False)


@dataclass(frozen=True)
class Thing:
    name: str
    fields: tuple[Field, ...]
    pos: Pos = field(default=NO_POS, compare=False)


@dataclass(frozen=True)
class Decided:
    """A ``# decided:`` line. Written by the compiler; a hard constraint."""

    text: str
    pos: Pos = field(default=NO_POS, compare=False)


@dataclass(frozen=True)
class Soft:
    """Text after ``~``: guidance the model may interpret freely."""

    text: str
    pos: Pos = field(default=NO_POS, compare=False)


@dataclass(frozen=True)
class Step:
    text: str
    soft: Soft | None = None
    decided: tuple[Decided, ...] = ()
    pos: Pos = field(default=NO_POS, compare=False)


@dataclass(frozen=True)
class Trigger:
    """An ``every ...`` line: when the job runs."""

    text: str
    decided: tuple[Decided, ...] = ()
    pos: Pos = field(default=NO_POS, compare=False)


@dataclass(frozen=True)
class Rule:
    kind: RuleKind
    text: str
    decided: tuple[Decided, ...] = ()
    pos: Pos = field(default=NO_POS, compare=False)

    @property
    def sentence(self) -> str:
        return f"{self.kind} {self.text}"


@dataclass(frozen=True)
class Exact:
    """Raw Python passed through verbatim. No LLM touches it."""

    code: str
    pos: Pos = field(default=NO_POS, compare=False)


DoesLine = Step | Trigger | Rule | Exact


@dataclass(frozen=True)
class Example:
    given: str
    expect: tuple[str, ...]  # one entry per comma-separated clause
    target: str | None = None  # the text after ``example for``, if any
    pos: Pos = field(default=NO_POS, compare=False)


@dataclass(frozen=True)
class Does:
    description: str
    lines: tuple[DoesLine, ...]
    decided: tuple[Decided, ...] = ()  # block-level decisions, before the first line
    examples: tuple[Example, ...] = ()
    pos: Pos = field(default=NO_POS, compare=False)

    @property
    def slug(self) -> str:
        return slugify(self.description)

    @property
    def steps(self) -> tuple[Step, ...]:
        return tuple(x for x in self.lines if isinstance(x, Step))

    @property
    def rules(self) -> tuple[Rule, ...]:
        return tuple(x for x in self.lines if isinstance(x, Rule))

    @property
    def trigger(self) -> Trigger | None:
        return next((x for x in self.lines if isinstance(x, Trigger)), None)

    @property
    def exacts(self) -> tuple[Exact, ...]:
        return tuple(x for x in self.lines if isinstance(x, Exact))

    def rule_ids(self) -> list[tuple[str, Rule]]:
        return [(f"R{i}", r) for i, r in enumerate(self.rules, start=1)]

    def all_decided(self) -> list[Decided]:
        out = list(self.decided)
        for line in self.lines:
            if isinstance(line, Step | Trigger | Rule):
                out.extend(line.decided)
        return out


@dataclass(frozen=True)
class Program:
    app: App
    things: tuple[Thing, ...] = ()
    does: tuple[Does, ...] = ()
    exacts: tuple[Exact, ...] = ()  # top-level exact blocks

    def thing(self, name: str) -> Thing | None:
        return next((t for t in self.things if t.name == name), None)

    def does_block(self, slug: str) -> Does | None:
        return next((d for d in self.does if d.slug == slug), None)


def slugify(text: str) -> str:
    """``chase overdue invoices`` -> ``chase_overdue_invoices`` (a valid Python identifier)."""
    slug = re.sub(r"[^0-9a-zA-Z]+", "_", text).strip("_").lower()
    if not slug:
        return "block"
    if slug[0].isdigit():
        slug = f"do_{slug}"
    return slug


def snake(name: str) -> str:
    """``InvoiceChaser`` -> ``invoice_chaser``."""
    s = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name)
    return slugify(s)


# --- (de)serialization --------------------------------------------------------------------

_NODE_TYPES: dict[str, type[Any]] = {
    cls.__name__: cls
    for cls in (
        Pos,
        Target,
        App,
        TypeRef,
        Field,
        Thing,
        Decided,
        Soft,
        Step,
        Trigger,
        Rule,
        Exact,
        Example,
        Does,
        Program,
    )
}


def to_data(node: Any, *, positions: bool = True) -> Any:
    """Convert a node tree to plain JSON-able data, tagging each node with its class name."""
    if dataclasses.is_dataclass(node) and not isinstance(node, type):
        out: dict[str, Any] = {"node": type(node).__name__}
        for f in dataclasses.fields(node):
            if f.name == "pos" and not positions:
                continue
            out[f.name] = to_data(getattr(node, f.name), positions=positions)
        return out
    if isinstance(node, tuple | list):
        return [to_data(x, positions=positions) for x in node]
    return node


def from_data(data: Any) -> Any:
    """Inverse of :func:`to_data`. Missing ``pos`` fields fall back to ``NO_POS``."""
    if isinstance(data, dict) and "node" in data:
        cls = _NODE_TYPES[data["node"]]
        kwargs = {k: from_data(v) for k, v in data.items() if k != "node"}
        return cls(**kwargs)
    if isinstance(data, list):
        return tuple(from_data(x) for x in data)
    return data
