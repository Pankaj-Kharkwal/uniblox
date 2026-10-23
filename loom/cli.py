"""The ``loom`` command line."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from loom import __version__
from loom.errors import LoomError
from loom.frontend import load

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Loom: compile plain-language intent, hard rules and examples into locked Python.",
)

FileArg = Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="A .loom file")]


def _fail(err: LoomError) -> None:
    typer.echo(err.render(), err=True)
    raise typer.Exit(1)


@app.command()
def check(
    file: FileArg,
    no_scan: Annotated[
        bool, typer.Option("--no-scan", help="Skip the LLM ambiguity scan (parse + types only).")
    ] = False,
) -> None:
    """Parse and type-check a .loom file. Exit 1 on any error."""
    try:
        fe = load(file)
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


@app.command()
def version() -> None:
    """Print the Loom version."""
    typer.echo(__version__)


def main() -> None:
    app()
