"""Canonical Loom text rendered from the AST.

Prompts are built from this rendering rather than from the raw file, so reformatting or
commenting a .loom file never changes a prompt, and therefore never changes a cache key.
"""

from __future__ import annotations

from loom.nodes import (
    App,
    Decided,
    Does,
    DoesLine,
    Exact,
    Example,
    Field,
    Program,
    Rule,
    Step,
    Thing,
    Trigger,
    TypeRef,
)


def render_type(t: TypeRef) -> str:
    if t.base == "one of":
        return "one of " + ", ".join(t.options)
    out = t.base + (f" ({t.arg})" if t.arg else "")
    return out + ("?" if t.optional else "")


def render_field(f: Field) -> str:
    return f"{f.name}: {render_type(f.type)}" + (f" = {f.default}" if f.default else "")


def render_app(app: App) -> str:
    t = app.target
    parts = [f"{t.language} {t.version}", *t.frameworks, *([t.database] if t.database else [])]
    return f"app {app.name}\ntarget: {', '.join(parts)}"


def render_thing(t: Thing) -> str:
    return "\n".join([f"thing {t.name}", *(f"  {render_field(f)}" for f in t.fields)])


def render_exact(e: Exact, indent: str = "") -> str:
    body = "\n".join(f"{indent}  {line}" if line else "" for line in e.code.split("\n"))
    return f"{indent}exact {{\n{body}\n{indent}}}"


def _decided(items: tuple[Decided, ...], indent: str) -> list[str]:
    return [f"{indent}# decided: {d.text}" for d in items]


def render_line(line: DoesLine) -> str:
    if isinstance(line, Trigger):
        return f"every {line.text}"
    if isinstance(line, Rule):
        return line.sentence
    if isinstance(line, Step):
        return line.text + (f" ~ {line.soft.text}" if line.soft else "")
    return "exact { ... }"


def render_example(e: Example) -> str:
    head = "example" + (f" for {e.target}" if e.target else "")
    return f"{head}\n  given {e.given}\n  expect {', '.join(e.expect)}"


def render_does(d: Does, *, numbered: bool = False, examples: bool = True) -> str:
    """Render a does block. ``numbered`` prefixes each line with ``[L<i>]`` (and rules with
    their id) so the model can refer to lines precisely."""
    out = [f"does {d.description}"]
    out.extend(_decided(d.decided, "  "))
    rule_ids = {id(r): rid for rid, r in d.rule_ids()}
    for i, line in enumerate(d.lines):
        prefix = ""
        if numbered:
            prefix = f"[L{i}] "
            if isinstance(line, Rule):
                prefix += f"({rule_ids[id(line)]}) "
        if isinstance(line, Exact):
            out.append(render_exact(line, "  ") if not numbered else f"  {prefix}exact {{ ... }}")
            continue
        out.append(f"  {prefix}{render_line(line)}")
        if isinstance(line, Step | Trigger | Rule):
            out.extend(_decided(line.decided, "    "))
    if examples:
        for n, e in enumerate(d.examples, start=1):
            text = render_example(e)
            out.append("")
            out.append(f"# E{n}\n{text}" if numbered else text)
    return "\n".join(out)


def render_program(p: Program) -> str:
    chunks = [render_app(p.app)]
    chunks += [render_thing(t) for t in p.things]
    chunks += [render_exact(e) for e in p.exacts]
    chunks += [render_does(d) for d in p.does]
    return "\n\n".join(chunks) + "\n"
