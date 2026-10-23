"""Source text -> typed AST.

Pipeline: normalize newlines -> cut out ``exact { ... }`` bodies (indentation-aware, so any
Python survives) -> reject tab indentation -> Lark LALR parse with an indentation postlexer ->
transform to :mod:`loom.nodes` -> structural checks (example binding, duplicates, target).
Every error is a :class:`~loom.errors.LoomSyntaxError` with a line, a column and a hint.
"""

from __future__ import annotations

import difflib
import re
import textwrap
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from functools import cache
from pathlib import Path
from typing import Any

from lark import Lark, Token, Transformer, v_args
from lark.exceptions import (
    UnexpectedCharacters,
    UnexpectedEOF,
    UnexpectedInput,
    UnexpectedToken,
    VisitError,
)
from lark.indenter import Indenter

from loom.errors import LoomSyntaxError
from loom.nodes import (
    App,
    Decided,
    Does,
    DoesLine,
    Exact,
    Example,
    Field,
    Pos,
    Program,
    Rule,
    RuleKind,
    Soft,
    Step,
    Target,
    Thing,
    Trigger,
    TypeRef,
    slugify,
)

SUPPORTED_FRAMEWORKS = ("fastapi",)
SUPPORTED_DATABASES = ("sqlite",)
MIN_PYTHON = (3, 12)


# --- Lark setup ---------------------------------------------------------------------------


class _LoomIndenter(Indenter):
    NL_type = "_NL"
    OPEN_PAREN_types: list[str] = []  # noqa: RUF012 - required by Lark's Indenter API
    CLOSE_PAREN_types: list[str] = []  # noqa: RUF012
    INDENT_type = "_INDENT"
    DEDENT_type = "_DEDENT"
    tab_len = 4

    def handle_NL(self, token: Token) -> Iterator[Token]:
        # Same as Lark's, but a bad dedent becomes a positioned LoomSyntaxError.
        yield token
        indent_str = token.rsplit("\n", 1)[1]
        indent = len(indent_str)
        if indent > self.indent_level[-1]:
            self.indent_level.append(indent)
            yield Token.new_borrow_pos(self.INDENT_type, indent_str, token)
            return
        levels = " or ".join(str(n) for n in self.indent_level)
        while indent < self.indent_level[-1]:
            self.indent_level.pop()
            yield Token.new_borrow_pos(self.DEDENT_type, indent_str, token)
        if indent != self.indent_level[-1]:
            raise LoomSyntaxError(
                f"inconsistent indentation: this line is indented by {indent} spaces, "
                "which matches no enclosing block",
                (token.end_line or 1),
                indent + 1,
                hint=f"use one of the indentation levels already open: {levels} spaces",
            )


@cache
def _lark() -> Lark:
    grammar = (Path(__file__).parent / "grammar.lark").read_text()
    return Lark(
        grammar,
        parser="lalr",
        lexer="contextual",
        postlex=_LoomIndenter(),
        propagate_positions=True,
        maybe_placeholders=False,
    )


# --- pre-passes ---------------------------------------------------------------------------

_EXACT_OPEN = re.compile(r"^(?P<indent>[ \t]*)exact[ \t]*\{(?P<rest>.*)$")


def _cut_exact(text: str) -> tuple[str, dict[int, Exact]]:
    """Replace each ``exact { ... }`` with an ``exact@N`` placeholder line.

    Body lines become blank lines so every later line keeps its line number. The closing
    ``}`` must sit alone on a line at the same indentation as ``exact``.
    """
    lines = text.split("\n")
    out: list[str] = []
    table: dict[int, Exact] = {}
    i = 0
    while i < len(lines):
        m = _EXACT_OPEN.match(lines[i])
        if not m:
            out.append(lines[i])
            i += 1
            continue
        indent = m["indent"]
        rest = m["rest"].strip()
        n = len(table) + 1
        pos = Pos(i + 1, len(indent) + 1)
        if rest.endswith("}"):  # one-liner: exact { code }
            table[n] = Exact(code=rest[:-1].strip(), pos=pos)
            out.append(f"{indent}exact@{n}")
            i += 1
            continue
        if rest:
            raise LoomSyntaxError(
                "code must start on the line after `exact {`",
                pos.line,
                pos.column,
                hint="write `exact {`, then the Python code indented below, then `}`",
            )
        j = i + 1
        while j < len(lines) and not (
            lines[j].strip() == "}" and lines[j][: len(lines[j]) - len(lines[j].lstrip())] == indent
        ):
            j += 1
        if j == len(lines):
            raise LoomSyntaxError(
                "unterminated `exact` block",
                pos.line,
                pos.column,
                hint="close it with `}` on its own line, lined up with `exact`",
            )
        body = [line.rstrip() for line in lines[i + 1 : j]]
        code = textwrap.dedent("\n".join(body)).strip("\n")
        table[n] = Exact(code=code, pos=pos)
        out.append(f"{indent}exact@{n}")
        out.extend([""] * (j - i))
        i = j + 1
    return "\n".join(out), table


def _reject_tabs(text: str) -> None:
    for lineno, line in enumerate(text.split("\n"), start=1):
        lead = line[: len(line) - len(line.lstrip(" \t"))]
        if "\t" in lead:
            raise LoomSyntaxError(
                "tab used for indentation",
                lineno,
                lead.index("\t") + 1,
                hint="indent with spaces (2 per level is the convention)",
            )


# --- tree -> AST --------------------------------------------------------------------------


@dataclass
class _Decisions:
    items: tuple[Decided, ...]


@dataclass
class _ExampleLine:
    kind: str  # "given" | "expect"
    text: str


def _pos_of(x: Any) -> Pos:
    if isinstance(x, Token):
        return Pos(x.line or 0, x.column or 0)
    return Pos(getattr(x, "line", 0) or 0, getattr(x, "column", 0) or 0)


def _clean(text: str) -> str:
    return " ".join(text.split())


def _split_clauses(text: str) -> tuple[str, ...]:
    return tuple(c.strip() for c in text.split(",") if c.strip())


@v_args(meta=True)
class _ToAst(Transformer[Token, Any]):
    def __init__(self, exacts: dict[int, Exact]) -> None:
        super().__init__()
        self._exacts = exacts

    # header
    def app(self, meta: Any, children: list[Token]) -> App:
        name, target = children
        return App(name=str(name), target=_parse_target(target), pos=_pos_of(meta))

    # things
    def thing(self, meta: Any, children: list[Any]) -> Thing:
        name, *fields = children
        return Thing(name=str(name), fields=tuple(fields), pos=_pos_of(meta))

    def field(self, meta: Any, children: list[Any]) -> Field:
        name, type_ref, *default = children
        return Field(
            name=str(name),
            type=type_ref,
            default=str(default[0]).strip() if default else None,
            pos=_pos_of(meta),
        )

    def simple_type(self, meta: Any, children: list[Any]) -> TypeRef:
        base, *rest = children
        arg = next((x for x in rest if isinstance(x, str) and not isinstance(x, Token)), None)
        optional = any(isinstance(x, Token) and x.type == "OPTIONAL" for x in rest)
        return TypeRef(base=str(base), arg=arg, optional=optional, pos=_pos_of(base))

    def type_arg(self, meta: Any, children: list[Token]) -> str:
        return str(children[0])

    def one_of(self, meta: Any, children: list[Token]) -> TypeRef:
        options = tuple(str(t) for t in children if t.type == "NAME")
        return TypeRef(base="one of", options=options, pos=_pos_of(meta))

    # does
    def does(self, meta: Any, children: list[Any]) -> Does:
        desc, *rest = children
        decided: tuple[Decided, ...] = ()
        if rest and isinstance(rest[0], _Decisions):
            decided = rest.pop(0).items
        lines: tuple[DoesLine, ...] = tuple(rest)
        return Does(description=_clean(desc), lines=lines, decided=decided, pos=_pos_of(meta))

    def trigger(self, meta: Any, children: list[Any]) -> Trigger:
        _kw, text, *dec = children
        return Trigger(text=_clean(text), decided=_items(dec), pos=_pos_of(meta))

    def rule(self, meta: Any, children: list[Any]) -> Rule:
        kw, text, *dec = children
        kind: RuleKind = "never" if kw.type == "NEVER" else "always"
        return Rule(kind=kind, text=_clean(text), decided=_items(dec), pos=_pos_of(meta))

    def step(self, meta: Any, children: list[Any]) -> Step:
        text, *rest = children
        soft = next((x for x in rest if isinstance(x, Soft)), None)
        dec = [x for x in rest if isinstance(x, _Decisions)]
        return Step(text=_clean(text), soft=soft, decided=_items(dec), pos=_pos_of(meta))

    def soft(self, meta: Any, children: list[Token]) -> Soft:
        return Soft(text=_clean(children[0]), pos=_pos_of(children[0]))

    def decided_lines(self, meta: Any, children: list[Token]) -> _Decisions:
        items = []
        for tok in children:
            text = tok.split(":", 1)[1]
            items.append(Decided(text=_clean(text), pos=_pos_of(tok)))
        return _Decisions(tuple(items))

    def decided_block(self, meta: Any, children: list[_Decisions]) -> _Decisions:
        return children[0]

    def inner_exact(self, meta: Any, children: list[Token]) -> Exact:
        return self._exacts[int(children[0].split("@")[1])]

    top_exact = inner_exact

    # examples
    def example(self, meta: Any, children: list[Any]) -> Example:
        target: str | None = None
        if children and isinstance(children[0], Token) and children[0].type == "FOR":
            target = _clean(children[1])
            children = children[2:]
        given = next(x.text for x in children if x.kind == "given")
        expect: tuple[str, ...] = ()
        for x in children:
            if x.kind == "expect":
                expect += _split_clauses(x.text)
        return Example(given=given, expect=expect, target=target, pos=_pos_of(meta))

    def given(self, meta: Any, children: list[Token]) -> _ExampleLine:
        return _ExampleLine("given", _clean(children[1]))

    def expect(self, meta: Any, children: list[Token]) -> _ExampleLine:
        return _ExampleLine("expect", children[1].strip())

    def start(self, meta: Any, children: list[Any]) -> list[Any]:
        return children


def _items(dec: list[Any]) -> tuple[Decided, ...]:
    return dec[0].items if dec else ()


def _parse_target(tok: Token) -> Target:
    raw = str(tok).rstrip()
    line, col = tok.line or 0, tok.column or 0
    parts: list[tuple[str, int]] = []
    offset = 0
    for chunk in raw.split(","):
        stripped = chunk.strip()
        parts.append((stripped.lower(), col + offset + (len(chunk) - len(chunk.lstrip()))))
        offset += len(chunk) + 1
    (lang_part, lang_col), *others = parts
    m = re.fullmatch(r"(?P<lang>[a-z]+)(?:\s+(?P<ver>\d+\.\d+))?", lang_part)
    if not m or m["lang"] != "python":
        raise LoomSyntaxError(
            f"unsupported target language {lang_part!r}",
            line,
            lang_col,
            hint="Loom v0 only targets python (TypeScript is on the roadmap): `python 3.12`",
        )
    version = m["ver"] or "3.12"
    if tuple(int(x) for x in version.split(".")) < MIN_PYTHON:
        raise LoomSyntaxError(
            f"python {version} is too old",
            line,
            lang_col,
            hint="generated code needs python 3.12 or newer",
        )
    frameworks: list[str] = []
    database: str | None = None
    for name, ccol in others:
        if name in SUPPORTED_FRAMEWORKS:
            frameworks.append(name)
        elif name in SUPPORTED_DATABASES:
            if database is not None:
                raise LoomSyntaxError("only one database per target", line, ccol)
            database = name
        else:
            known = SUPPORTED_FRAMEWORKS + SUPPORTED_DATABASES
            close = difflib.get_close_matches(name, known, n=1)
            hint = f"did you mean {close[0]!r}?" if close else f"supported: {', '.join(known)}"
            raise LoomSyntaxError(f"unknown target component {name!r}", line, ccol, hint=hint)
    return Target(
        language="python",
        version=version,
        frameworks=tuple(frameworks),
        database=database,
        pos=Pos(line, col),
    )


# --- error messages -----------------------------------------------------------------------

# Each label is a message; the snippets reproduce the parser state where it applies. Lark's
# ``match_examples`` compares the failing state against these to pick the best message.
_ERROR_EXAMPLES: dict[tuple[str, str], list[str]] = {
    (
        "a Loom file must start with `app <Name>`",
        "e.g. `app InvoiceChaser` followed by `target: python 3.12`",
    ): ["thing X\n", "does x\n", "example\n", "target: python 3.12\n"],
    (
        "expected `target: ...` on the line after `app`",
        "e.g. `target: python 3.12, fastapi, sqlite`",
    ): ["app X\nthing Y\n", "app X\n\n", "app X\ndoes y\n"],
    (
        "missing type after ':'",
        "e.g. `client: text` (types: text, email, number, money (CUR), "
        "date, datetime, one of a, b)",
    ): [
        "app X\ntarget: python 3.12\nthing T\n  a:\n",
        "app X\ntarget: python 3.12\nthing T\n  a: text\n  b:\n",
    ],
    (
        "`one of` needs at least one option",
        "e.g. `status: one of draft, sent, paid`",
    ): ["app X\ntarget: python 3.12\nthing T\n  a: one of\n"],
    (
        "a thing needs indented fields",
        "indent each `name: type` line under the `thing` header",
    ): ["app X\ntarget: python 3.12\nthing T\na: text\n", "app X\ntarget: python 3.12\nthing T\n"],
    (
        "a does block needs indented steps",
        "indent each step under the `does` header",
    ): [
        "app X\ntarget: python 3.12\ndoes x\nexample\n",
        "app X\ntarget: python 3.12\ndoes x\n",
    ],
    (
        "nothing after `~`",
        "write the soft preference after `~`, or drop the `~`",
    ): [
        "app X\ntarget: python 3.12\ndoes x\n  do y ~\n",
        "app X\ntarget: python 3.12\ndoes x\n  y\n  do y ~\n",
    ],
    (
        "`~` soft preferences are not allowed on never/always rules",
        "rules are hard constraints; move the preference to a step",
    ): [
        "app X\ntarget: python 3.12\ndoes x\n  never y ~ z\n",
        "app X\ntarget: python 3.12\ndoes x\n  always y ~ z\n",
        "app X\ntarget: python 3.12\ndoes x\n  y\n  never y ~ z\n",
        "app X\ntarget: python 3.12\ndoes x\n  y\n  always y ~ z\n",
    ],
    (
        "an example must start with a `given` line",
        "e.g. `given invoice due 3 days ago, status sent`",
    ): [
        "app X\ntarget: python 3.12\ndoes x\n  y\nexample\n  expect z\n",
        "app X\ntarget: python 3.12\ndoes x\n  y\nexample\n  hello\n",
    ],
    (
        "an example needs an `expect` line after `given`",
        "e.g. `expect status overdue, 1 email queued`",
    ): [
        "app X\ntarget: python 3.12\ndoes x\n  y\nexample\n  given z\n",
        "app X\ntarget: python 3.12\ndoes x\n  y\nexample\n  given z\n  hello\n",
    ],
    (
        "an example needs indented `given` and `expect` lines",
        "indent them under `example`",
    ): ["app X\ntarget: python 3.12\ndoes x\n  y\nexample\ngiven z\n"],
}

_FRIENDLY = {
    "NAME": "a name",
    "PROSE": "text",
    "_NL": "end of line",
    "_INDENT": "an indented line",
    "_DEDENT": "the end of the block",
    "$END": "end of file",
    "DEFAULT": "a default value",
    "TARGET": "a target",
    "DECIDED": "a `# decided:` line",
    "EXACT_REF": "an `exact` block",
    "COLON": "':'",
    "EQUAL": "'='",
    "LPAR": "'('",
    "RPAR": "')'",
    "COMMA": "','",
    "TILDE": "'~'",
    "THING": "`thing`",
    "DOES": "`does`",
    "EXAMPLE": "`example`",
}


def _friendly(names: set[str] | frozenset[str]) -> str:
    out = sorted({_FRIENDLY.get(n, n.lower().strip("_")) for n in names})
    return ", ".join(out) if out else "something else"


def _translate(exc: UnexpectedInput, parse_fn: Callable[[str], Any]) -> LoomSyntaxError:
    line, col = exc.line, exc.column
    examples: Any = _ERROR_EXAMPLES  # Lark's stub types the labels as Never
    label = exc.match_examples(parse_fn, examples, use_accepts=True, token_type_match_fallback=True)
    if label is not None:
        message, hint = label
        return LoomSyntaxError(message, max(line, 1), max(col, 1), hint=hint)
    if isinstance(exc, UnexpectedToken):
        tok = exc.token
        structural = ("_NL", "_INDENT", "_DEDENT", "$END")
        found = _FRIENDLY[tok.type] if tok.type in structural else repr(str(tok).strip())
        expected = _friendly(exc.accepts or exc.expected)
        return LoomSyntaxError(
            f"unexpected {found}; expected {expected}", max(line, 1), max(col, 1)
        )
    if isinstance(exc, UnexpectedCharacters):
        expected = _friendly(exc.allowed or set())
        return LoomSyntaxError(
            f"unexpected character {exc.char!r}; expected {expected}", max(line, 1), max(col, 1)
        )
    if isinstance(exc, UnexpectedEOF):
        return LoomSyntaxError("unexpected end of file", max(line, 1), max(col, 1))
    return LoomSyntaxError(str(exc), max(line, 1), max(col, 1))  # pragma: no cover


# --- public API ---------------------------------------------------------------------------


def _prepare(text: str) -> tuple[str, dict[int, Exact]]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.endswith("\n"):
        text += "\n"
    cut, exacts = _cut_exact(text)
    _reject_tabs(cut)
    return cut, exacts


def _raw_parse(text: str) -> Any:
    cut, _ = _prepare(text)
    return _lark().parse(cut)


def parse(text: str, path: str | None = None) -> Program:
    """Parse Loom source into a :class:`~loom.nodes.Program`.

    Raises :class:`LoomSyntaxError` (with ``source`` and ``path`` filled in) on any error.
    """
    try:
        cut, exacts = _prepare(text)
        try:
            tree = _lark().parse(cut)
        except UnexpectedInput as exc:
            raise _translate(exc, _raw_parse) from None
        try:
            app, *blocks = _ToAst(exacts).transform(tree)
        except VisitError as exc:
            if isinstance(exc.orig_exc, LoomSyntaxError):
                raise exc.orig_exc from None
            raise
        return _assemble(app, blocks)
    except LoomSyntaxError as err:
        err.source = text
        err.path = path
        raise


def parse_file(path: str | Path) -> Program:
    p = Path(path)
    return parse(p.read_text(encoding="utf-8"), path=str(p))


def _assemble(app: App, blocks: list[Any]) -> Program:
    things: list[Thing] = []
    does: list[Does] = []
    top_exacts: list[Exact] = []
    examples: dict[int, list[Example]] = {}
    pending_for: list[Example] = []

    for block in blocks:
        if isinstance(block, Thing):
            if any(t.name == block.name for t in things):
                raise LoomSyntaxError(
                    f"thing {block.name!r} is defined twice",
                    block.pos.line,
                    block.pos.column,
                    hint="merge the fields into one `thing` block",
                )
            seen: set[str] = set()
            for f in block.fields:
                if f.name in seen:
                    raise LoomSyntaxError(
                        f"field {f.name!r} appears twice in thing {block.name!r}",
                        f.pos.line,
                        f.pos.column,
                    )
                seen.add(f.name)
            things.append(block)
        elif isinstance(block, Does):
            _check_does(block, does)
            does.append(block)
        elif isinstance(block, Example):
            if block.target is not None:
                pending_for.append(block)
            elif not does:
                raise LoomSyntaxError(
                    "this example comes before any `does` block",
                    block.pos.line,
                    block.pos.column,
                    hint=(
                        "put examples after the does block they test, or write `example for <does>`"
                    ),
                )
            else:
                examples.setdefault(len(does) - 1, []).append(block)
        elif isinstance(block, Exact):
            top_exacts.append(block)

    for ex in pending_for:
        idx = _find_does(does, ex)
        examples.setdefault(idx, []).append(ex)

    bound = tuple(
        replace(d, examples=tuple(sorted(examples.get(i, []), key=lambda e: e.pos.line)))
        for i, d in enumerate(does)
    )
    return Program(app=app, things=tuple(things), does=bound, exacts=tuple(top_exacts))


def _check_does(block: Does, earlier: list[Does]) -> None:
    if any(d.slug == block.slug for d in earlier):
        raise LoomSyntaxError(
            f"another does block is also named {block.slug!r}",
            block.pos.line,
            block.pos.column,
            hint="give each does block a distinct description",
        )
    triggers = [x for x in block.lines if isinstance(x, Trigger)]
    if len(triggers) > 1:
        t = triggers[1]
        raise LoomSyntaxError(
            "a does block can have only one `every ...` trigger", t.pos.line, t.pos.column
        )
    if not block.steps:
        raise LoomSyntaxError(
            "a does block needs at least one step besides rules and triggers",
            block.pos.line,
            block.pos.column,
            hint="describe what it does, one step per line",
        )


def _find_does(does: list[Does], ex: Example) -> int:
    assert ex.target is not None
    want = ex.target.lower()
    for i, d in enumerate(does):
        if d.description.lower() == want or d.slug == slugify(want):
            return i
    names = [d.description for d in does]
    close = difflib.get_close_matches(ex.target, names, n=1)
    raise LoomSyntaxError(
        f"`example for {ex.target}` doesn't match any does block",
        ex.pos.line,
        ex.pos.column,
        hint=f"did you mean {close[0]!r}?"
        if close
        else f"does blocks: {', '.join(names) or 'none'}",
    )
