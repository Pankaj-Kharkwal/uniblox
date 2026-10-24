"""The ``loom`` command line."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Annotated

import typer

from loom import __version__
from loom.ambiguity import Ambiguity, settle
from loom.build import BuildOptions, BuildReport, build, default_out_dir
from loom.diff import diff as intent_diff
from loom.eject import eject as eject_project
from loom.errors import LoomError
from loom.frontend import load
from loom.llm import make_client
from loom.lock import Lock, lock_path
from loom.nodes import Does
from loom.render import render_line
from loom.runner import RunResult, run_pytest
from loom.scaffold import project_for

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Loom: compile plain-language intent, hard rules and examples into locked Python.",
)

FileArg = Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="A .loom file")]
ModelOpt = Annotated[
    str | None,
    typer.Option("--model", help="Claude model (default: $LOOM_MODEL or claude-sonnet-5-5)."),
]
BackendOpt = Annotated[
    str | None,
    typer.Option(
        "--backend",
        help="Model backend: api (ANTHROPIC_API_KEY), claude-code (your Claude subscription via "
        "the claude CLI) or auto (default: $LOOM_BACKEND, else api if a key is set, else "
        "claude-code if installed).",
    ),
]
YesOpt = Annotated[
    bool, typer.Option("--yes", "-y", help="Accept the suggested answer to every ambiguity.")
]


def _interactive() -> bool:
    return os.environ.get("LOOM_INTERACTIVE") == "1" or sys.stdin.isatty()


def _ask(amb: Ambiguity, does: Does) -> str | None:
    """Ask the user one ambiguity question on the terminal."""
    typer.echo("")
    where = f"does {does.description}"
    if 0 <= amb.line < len(does.lines):
        where += f' · "{render_line(does.lines[amb.line])}"'
    typer.secho(f"? {where}", bold=True)
    typer.echo(f"  {amb.question}")
    if amb.why:
        typer.echo(f"  why it matters: {amb.why}")
    for i, option in enumerate(amb.options, start=1):
        marker = "  (suggested)" if i - 1 == amb.suggested else ""
        typer.echo(f"   {i}) {option}{marker}")
    reply: str = typer.prompt("  answer (number, or type your own)", default=str(amb.suggested + 1))
    reply = reply.strip()
    if reply.isdigit() and 1 <= int(reply) <= len(amb.options):
        return amb.options[int(reply) - 1]
    return reply or None


def _fail(err: LoomError) -> None:
    typer.echo(err.render(), err=True)
    raise typer.Exit(1)


@app.command()
def check(
    file: FileArg,
    no_scan: Annotated[
        bool, typer.Option("--no-scan", help="Skip the LLM ambiguity scan (parse + types only).")
    ] = False,
    yes: YesOpt = False,
    model: ModelOpt = None,
    backend: BackendOpt = None,
) -> None:
    """Parse, type-check and scan for ambiguities, without generating code.

    Exit 1 on any error, including an ambiguity left unanswered.
    """
    try:
        fe = load(file)
        if not no_scan:
            llm = make_client(model, backend)
            fe, answers = settle(
                file, fe, llm, fe.program.does, yes=yes, ask=_ask if _interactive() else None
            )
            for a in answers:
                typer.echo(f"decided ({a.block}): {a.text}")
    except LoomError as err:
        _fail(err)
        return
    program, things = fe.program, fe.things
    n_examples = sum(len(d.examples) for d in program.does)
    n_rules = sum(len(d.rules) for d in program.does)
    typer.echo(
        f"ok: app {program.app.name}: {len(things)} things, {len(program.does)} does blocks, "
        f"{n_rules} rules, {n_examples} examples"
    )


OutOpt = Annotated[
    Path | None,
    typer.Option("--out", help="Output directory (default: ./out/<app_name>)."),
]


def _summary(result: RunResult) -> str:
    counts = result.counts()
    parts = [f"{counts.get(k, 0)} {k}" for k in ("passed", "failed", "error") if counts.get(k)]
    return ", ".join(parts) or "no tests ran"


def _red_report(report: BuildReport) -> None:
    loop = report.loop
    assert loop is not None
    result = loop.result
    typer.secho(
        f"RED: still failing after {loop.rounds} repair round(s) (--max-iters reached)"
        if loop.rounds
        else "RED: the generated tests do not pass",
        fg="red",
        bold=True,
        err=True,
    )
    by_block: dict[str, list[str]] = {}
    for case in result.failures:
        first = case.message.splitlines()[0] if case.message else case.outcome
        if case.broken_test:
            first = f"the generated test itself cannot run ({first})"
        by_block.setdefault(case.block or case.file, []).append(
            f"{case.file}::{case.name}: {first}"
        )
    for block, lines in sorted(by_block.items()):
        tries = loop.iterations.get(block, 0)
        typer.echo(f"  does {block} ({tries} repair attempt(s)):", err=True)
        for line in lines:
            typer.echo(f"    - {line}", err=True)
        for err in loop.repair_errors.get(block, []):
            typer.echo(f"    ! rejected repair: {err.splitlines()[0]}", err=True)
    if not by_block:
        typer.echo(result.output[-3000:], err=True)
    typer.echo(
        f"  {len(loop.test_hashes)} locked test file(s) verified unchanged. The tests come from "
        "your examples and rules: if one of them is wrong, fix it in the .loom file.",
        err=True,
    )


@app.command("build")
def build_cmd(
    file: FileArg,
    yes: YesOpt = False,
    model: ModelOpt = None,
    backend: BackendOpt = None,
    max_iters: Annotated[
        int, typer.Option("--max-iters", min=1, help="Repair attempts per failing block.")
    ] = 4,
    force: Annotated[
        bool, typer.Option("--force", help="Rebuild every block, overwriting hand edits.")
    ] = False,
    out: OutOpt = None,
) -> None:
    """Compile a .loom file into a tested Python project and write <file>.lock."""
    opts = BuildOptions(
        out_dir=out,
        yes=yes,
        ask=_ask if _interactive() else None,
        max_iters=max_iters,
        force=force,
        echo=typer.echo,
    )
    try:
        report = build(file, lambda: make_client(model, backend), opts)
    except LoomError as err:
        _fail(err)
        return
    if report.nothing_to_rebuild:
        typer.echo(f"nothing to rebuild: {file.name} matches {report.lock_file.name}")
        return
    assert report.result is not None
    calls = report.calls.get("api_calls", 0)
    if not report.ok:
        _red_report(report)
        raise typer.Exit(1)
    regenerated = ", ".join(report.regenerated) or "none"
    typer.secho(f"GREEN: {_summary(report.result)} in {report.project_dir}", fg="green")
    typer.echo(
        f"regenerated: {regenerated} · repair rounds: {report.loop.rounds if report.loop else 0}"
        f" · model calls: {calls} · lock: {report.lock_file.name}"
    )


@app.command("test")
def test_cmd(file: FileArg, out: OutOpt = None) -> None:
    """Run the generated project's pytest suite (no code generation)."""
    try:
        fe = load(file)
    except LoomError as err:
        _fail(err)
        return
    project = out or default_out_dir(project_for(fe))
    if not (project / "tests").is_dir():
        typer.echo(f"no generated project at {project}; run `loom build {file}` first", err=True)
        raise typer.Exit(1)
    result = run_pytest(project)
    typer.echo(result.output.rstrip())
    if not result.ok:
        raise typer.Exit(1)


@app.command("diff")
def diff_cmd(
    file: FileArg,
    out: OutOpt = None,
    exit_code: Annotated[
        bool, typer.Option("--exit-code", help="Exit 1 if anything would regenerate (for CI).")
    ] = False,
) -> None:
    """Show the intent diff against the lock, then which blocks would regenerate."""
    try:
        fe = load(file)
        lf = lock_path(file)
        report = intent_diff(fe, Lock.load(lf), lf, out)
    except LoomError as err:
        _fail(err)
        return
    if not report.has_lock:
        typer.echo(f"No {lf.name} yet: the first build generates every block.\n")
    elif report.empty:
        typer.echo(f"No intent changes since the last build: {file.name} matches {lf.name}.")
        return
    else:
        typer.secho("Intent diff", bold=True)
        if not report.changes:
            typer.echo("  (no changes to the spec)")
        current = None
        for change in report.changes:
            if change.block != current:
                typer.echo(f"  {change.block}")
                current = change.block
            typer.echo(f"    - {change.sentence}")
        typer.echo("")
    typer.secho("Code impact", bold=True)
    plan = report.plan
    assert plan is not None
    if not plan.dirty:
        typer.echo("  no does block regenerates (no model calls)")
    for slug, reason in plan.dirty.items():
        typer.echo(f"  does {slug} regenerates ({reason}):")
        for rel in report.files.get(slug, []):
            typer.echo(f"    {rel}")
    for key in plan.removed:
        typer.echo(f"  {key.replace(':', ' ', 1)} was removed: its generated files are deleted")
    if plan.shared_changed and report.has_lock:
        typer.echo("  shared files (models, runtime, scaffold) are rewritten (no model calls)")
    if plan.hand_edited:
        typer.echo("  hand-edited generated files (a build will refuse until --force):")
        for rel in plan.hand_edited:
            typer.echo(f"    {rel}")
    if exit_code:
        raise typer.Exit(1)


@app.command("eject")
def eject_cmd(
    file: FileArg,
    out: OutOpt = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")] = False,
    run_tests: Annotated[
        bool, typer.Option("--test/--no-test", help="Run the project's tests afterwards.")
    ] = True,
) -> None:
    """Strip Loom metadata and leave a plain Python project (deletes <file>.lock)."""
    try:
        fe = load(file)
    except LoomError as err:
        _fail(err)
        return
    lf = lock_path(file)
    lock = Lock.load(lf)
    project = out or (lock.resolve_out_dir(lf) if lock else default_out_dir(project_for(fe)))
    if not yes:
        if not _interactive():
            typer.echo("eject deletes the lockfile and Loom's file headers; pass --yes", err=True)
            raise typer.Exit(1)
        typer.confirm(
            f"Eject {project}? This deletes {lf.name}; later builds will not manage it.",
            abort=True,
        )
    try:
        report = eject_project(project, lf)
    except LoomError as err:
        _fail(err)
        return
    typer.echo(
        f"Ejected {project}: removed headers from {len(report.stripped)} files"
        + (f", deleted {lf.name}" if report.lock_deleted else "")
        + (f", removed {len(report.removed)} cache folder(s)" if report.removed else "")
        + "."
    )
    if report.leftovers:
        typer.echo("These files still mention Loom (your own code?):", err=True)
        for rel in report.leftovers:
            typer.echo(f"  {rel}", err=True)
    else:
        typer.echo("No Loom references remain.")
    if run_tests:
        result = run_pytest(project)
        for leftover in ("__pycache__", ".hypothesis"):
            for path in project.rglob(leftover):
                shutil.rmtree(path, ignore_errors=True)
        typer.echo(f"pytest: {_summary(result)}")
        if not result.ok:
            raise typer.Exit(1)


@app.command()
def version() -> None:
    """Print the Loom version."""
    typer.echo(__version__)


def main() -> None:
    app()
