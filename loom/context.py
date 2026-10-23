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


_RUNTIME_API = """\
## {pkg}.runtime
- `guard(condition, rule_id, detail="")`: raise `RuleViolation` when `condition` is falsy.
- `RuleViolation`: subclass of `AssertionError`; has `.rule_id` and `.rule` (the rule text).
- `utcnow()`: the current time as an aware UTC datetime.
"""

_OUTBOX_DB_API = """\
## {pkg}.outbox (a database table: emails are queued, never sent)
- `OutboxEmail`: SQLModel table `outbox` with `id`, `to`, `subject`, `body`, `queued_at`
  (aware datetime) and `ref` (free text saying what the email is about, e.g. "Invoice:3").
- `queue_email(session, *, to, subject, body, now, ref="") -> OutboxEmail`: add and flush.
- `queued_emails(session, *, to=None) -> list[OutboxEmail]`: queued emails, oldest first.

## {pkg}.db
- `memory_session()`: context manager yielding a `Session` on a fresh in-memory database with
  every table created. For tests.
- `session_scope()`: context manager yielding a production `Session` that commits on success.
- Datetime columns store aware datetimes and return them aware, in UTC.
"""

_OUTBOX_MEMORY_API = """\
## {pkg}.outbox (in memory: emails are queued, never sent)
- `Outbox()`: holds `emails: list[OutboxEmail]`; `.queue_email(*, to, subject, body, now,
  ref="")` appends one; `.queued_emails(*, to=None)` lists them.
"""

_FIXTURES = """\
## Test fixtures (tests/conftest.py)
- `now`: a fixed, timezone-aware datetime, 2026-03-11T04:30:00+00:00, which is Wednesday
  2026-03-11 10:00 in Asia/Kolkata.
{session}- Hypothesis runs derandomized with 50 examples per test, no deadline, and function-scoped
  fixtures allowed.
"""

_IMPORTS = """\
## Allowed imports
The Python standard library (except os, sys, subprocess, socket, shutil, pathlib, importlib,
threading, multiprocessing, urllib, http, pickle and builtins), pydantic, sqlmodel,
sqlalchemy, the package itself, and in tests also pytest and hypothesis.
"""


def project_context(fe: Frontend, package: str, models_source: str, has_db: bool) -> str:
    """Spec context plus what the generated project already provides.

    Shared by every codegen and test call of a project, so it is cached across blocks.
    """
    out = [spec_context(fe), "# Generated project", ""]
    out.append(
        f"Python package `{package}` (src layout). Use absolute imports, for example "
        f"`from {package}.models import ...`."
    )
    out += ["", f"## {package}.models (generated from the things; never redefine them)", ""]
    out += ["```python", models_source.rstrip(), "```", ""]
    out.append(_RUNTIME_API.format(pkg=package))
    out.append((_OUTBOX_DB_API if has_db else _OUTBOX_MEMORY_API).format(pkg=package))
    if fe.program.exacts:
        out.append(f"## {package}.exact: the top-level exact helpers above, importable from here\n")
    session = "- `session`: a fresh in-memory database `Session` with every table created.\n"
    out.append(_FIXTURES.format(session=session if has_db else ""))
    out.append(_IMPORTS)
    return "\n".join(out).rstrip() + "\n"
