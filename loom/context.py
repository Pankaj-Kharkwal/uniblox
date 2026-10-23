"""Prompt context shared by every model call for a project."""

from __future__ import annotations

from pathlib import Path

from loom.frontend import Frontend
from loom.render import render_app, render_exact, render_field

PROMPTS = Path(__file__).parent / "prompts"


def prompt(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8")


def system_prompt(kind: str) -> str:
    """The cached instructions for one kind of call: the shared Loom primer plus the task."""
    return prompt("common") + "\n\n" + prompt(kind)


def spec_context(fe: Frontend) -> str:
    """App header, things with resolved Python types, and top-level exact helpers.

    Built from the AST, so formatting edits to the .loom file never change it.
    """
    p = fe.program
    out = ["# Project", "", render_app(p.app), ""]
    if p.things:
        out += ["# Things (data shapes) with their resolved Python types", ""]
        for t in p.things:
            resolved = {f.name: f for f in fe.things[t.name].fields}
            out.append(f"thing {t.name}")
            for f in t.fields:
                rt = resolved[f.name].type
                py = rt.py + (f"  (currency {rt.currency})" if rt.currency else "")
                out.append(f"  {render_field(f):<40} -> {py}")
            out.append("")
    if p.exacts:
        out += [
            "# Top-level exact helpers (verbatim Python, importable from the `exact` module)",
            "",
        ]
        out += [render_exact(e) for e in p.exacts]
        out.append("")
    return "\n".join(out).rstrip() + "\n"
