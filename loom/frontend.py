"""Parse + type resolution, with error positions tied back to the source file."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from loom.errors import LoomSyntaxError
from loom.nodes import Program
from loom.parser import parse
from loom.typesys import ResolvedThing, resolve_program


@dataclass(frozen=True)
class Frontend:
    program: Program
    things: dict[str, ResolvedThing]
    source: str
    path: str | None


def load_text(source: str, path: str | None = None) -> Frontend:
    program = parse(source, path)
    try:
        things = resolve_program(program)
    except LoomSyntaxError as err:
        err.source, err.path = source, path
        raise
    return Frontend(program=program, things=things, source=source, path=path)


def load(path: str | Path) -> Frontend:
    p = Path(path)
    return load_text(p.read_text(encoding="utf-8"), str(p))
