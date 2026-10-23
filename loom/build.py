"""The ``loom build`` pipeline: settle ambiguities, scaffold, generate, translate tests, run."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from loom.ambiguity import Resolver, settle
from loom.context import project_context
from loom.frontend import Frontend, load
from loom.generate import GeneratedModule, generate_block, glue_for
from loom.llm import LLM
from loom.runner import RunResult, run_pytest
from loom.scaffold import Project, glue_files, scaffold, strip_header
from loom.tests_gen import example_tests, rule_tests


@dataclass
class BuildOptions:
    out_dir: Path | None = None
    yes: bool = False
    ask: Resolver | None = None
    max_iters: int = 4
    force: bool = False
    echo: Callable[[str], None] = print


@dataclass
class BuildReport:
    project_dir: Path
    package: str
    regenerated: list[str] = field(default_factory=list)
    result: RunResult | None = None

    @property
    def ok(self) -> bool:
        return self.result is not None and self.result.ok


def default_out_dir(proj: Project) -> Path:
    return Path.cwd() / "out" / proj.package


def write_files(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and not path.stat().st_mode & 0o200:
            path.chmod(0o644)  # locked tests are read-only; a rebuild may replace them
        path.write_text(content, encoding="utf-8")


def models_source(proj: Project) -> str:
    return strip_header(proj.files[f"src/{proj.package}/models.py"])


def build(path: Path, llm: LLM, opts: BuildOptions) -> BuildReport:
    echo = opts.echo
    fe: Frontend = load(path)
    fe, answers = settle(path, fe, llm, fe.program.does, yes=opts.yes, ask=opts.ask)
    for a in answers:
        echo(f"decided ({a.block}): {a.text}")

    proj = scaffold(fe)
    out = opts.out_dir or default_out_dir(proj)
    context = project_context(fe, proj.package, models_source(proj), proj.has_db)
    report = BuildReport(project_dir=out, package=proj.package)

    modules: dict[str, GeneratedModule] = {}
    for does in fe.program.does:
        echo(f"generating does {does.slug}")
        others = {slug: m.signature for slug, m in modules.items()}
        modules[does.slug] = generate_block(llm, fe, does, proj, others, context=context)
        report.regenerated.append(does.slug)

    files = dict(proj.files)
    for does in fe.program.does:
        m = modules[does.slug]
        files[m.path] = proj.header(f"does {does.slug}") + m.source
        echo(f"translating examples and rules of {does.slug} into tests")
        ex = example_tests(llm, fe, does, proj, m.signature, context=context)
        if ex is not None:
            files[proj.test_path(does.slug, "examples")] = (
                proj.header(f"examples of does {does.slug}") + ex
            )
        ru = rule_tests(llm, fe, does, proj, m.signature, context=context)
        if ru is not None:
            files[proj.test_path(does.slug, "rules")] = (
                proj.header(f"rules of does {does.slug}") + ru
            )
    files.update(glue_files(fe, proj, [glue_for(m) for m in modules.values()]))
    write_files(out, files)

    echo("running the generated tests")
    report.result = run_pytest(out)
    return report
