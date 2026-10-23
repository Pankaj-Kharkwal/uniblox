"""The ``loom build`` pipeline.

1. parse + types; load ``<file>.lock``; refuse to overwrite hand-edited generated files
2. plan: which does blocks are dirty (new, intent changed, shared context changed, red last
   time, files missing). Nothing dirty -> "nothing to rebuild", with no model client created.
3. settle ambiguities of the dirty blocks (``# decided:`` written back into the source)
4. scaffold (deterministic) and regenerate the dirty blocks: code, example tests, rule tests
5. green loop: run the suite, repair only failing blocks, verify tests never changed
6. write the lock
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from loom.ambiguity import Resolver, settle
from loom.context import project_context
from loom.errors import GenerationError, LockError
from loom.frontend import Frontend, load
from loom.generate import GeneratedModule, generate_block, glue_for, repair_block
from loom.hashing import does_input_hash, node_hash, shared_context_hash
from loom.llm import LLM
from loom.lock import (
    BlockLock,
    Lock,
    content_hash,
    drifted_files,
    lock_path,
    now_iso,
    relative_out_dir,
)
from loom.loop import LoopOutcome, green_loop
from loom.nodes import to_data
from loom.runner import RunResult
from loom.scaffold import BlockGlue, Project, Schedule, glue_files, scaffold, strip_header
from loom.tests_gen import example_tests, rule_tests
from loom.validate import function_info

LLMFactory = Callable[[], LLM]


@dataclass
class BuildOptions:
    out_dir: Path | None = None
    yes: bool = False
    ask: Resolver | None = None
    max_iters: int = 4
    force: bool = False
    echo: Callable[[str], None] = print


@dataclass
class BuildPlan:
    dirty: dict[str, str] = field(default_factory=dict)  # does slug -> reason
    removed: list[str] = field(default_factory=list)  # lock block keys no longer in the source
    shared_changed: bool = False
    hand_edited: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def nothing_to_do(self) -> bool:
        return not self.dirty and not self.removed and not self.shared_changed


@dataclass
class BuildReport:
    project_dir: Path
    package: str
    lock_file: Path
    plan: BuildPlan
    regenerated: list[str] = field(default_factory=list)
    loop: LoopOutcome | None = None
    calls: dict[str, int] = field(default_factory=dict)

    @property
    def result(self) -> RunResult | None:
        return self.loop.result if self.loop else None

    @property
    def ok(self) -> bool:
        return self.loop is not None and self.loop.ok

    @property
    def nothing_to_rebuild(self) -> bool:
        return self.loop is None


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


def _does_key(slug: str) -> str:
    return f"does:{slug}"


def plan_build(
    fe: Frontend, proj: Project, lock: Lock | None, out: Path, *, force: bool
) -> BuildPlan:
    """Decide what to regenerate. Pure: reads the lock and the output directory only."""
    plan = BuildPlan()
    program = fe.program
    if lock is None or force:
        reason = "--force" if force and lock is not None else "first build"
        plan.dirty = {d.slug: reason for d in program.does}
        plan.shared_changed = True
        if lock is not None:
            current = {_does_key(d.slug) for d in program.does} | {
                f"thing:{t.name}" for t in program.things
            }
            plan.removed = sorted(k for k in lock.blocks if k not in current)
        return plan

    changed, missing = drifted_files(out, lock.all_files())
    plan.hand_edited, plan.missing = changed, missing

    shared = shared_context_hash(program)
    shared_changed = shared != lock.shared_hash
    expected_shared = {k: content_hash(v) for k, v in proj.files.items()}
    locked_scaffold = {k: v for k, v in lock.shared_files.items() if k in expected_shared}
    plan.shared_changed = (
        shared_changed
        or locked_scaffold != expected_shared
        or any(m in lock.shared_files for m in missing)
    )

    for does in program.does:
        entry = lock.blocks.get(_does_key(does.slug))
        if entry is None:
            plan.dirty[does.slug] = "new block"
        elif shared_changed:
            plan.dirty[does.slug] = "things, app header or exact code changed"
        elif entry.input_hash != does_input_hash(program, does):
            plan.dirty[does.slug] = "intent changed"
        elif entry.status != "green":
            plan.dirty[does.slug] = "red in the last build"
        elif any(f in missing for f in entry.all_files()):
            plan.dirty[does.slug] = "generated files are missing"

    current = {_does_key(d.slug) for d in program.does} | {
        f"thing:{t.name}" for t in program.things
    }
    plan.removed = sorted(k for k in lock.blocks if k not in current)
    return plan


def _check_hand_edits(plan: BuildPlan, force: bool) -> None:
    if plan.hand_edited and not force:
        listing = "\n".join(f"  - {f}" for f in plan.hand_edited)
        raise LockError(
            "these generated files were edited by hand since the last build:\n"
            f"{listing}\n"
            "Change the .loom file instead (the code is regenerated from it), or run "
            "`loom build --force` to overwrite the edits."
        )


def build(path: Path, llm_factory: LLMFactory, opts: BuildOptions) -> BuildReport:
    echo = opts.echo
    fe: Frontend = load(path)
    lf = lock_path(path)
    lock = Lock.load(lf)
    proj = scaffold(fe)
    out = opts.out_dir or (lock.resolve_out_dir(lf) if lock else default_out_dir(proj))

    plan = plan_build(fe, proj, lock, out, force=opts.force)
    _check_hand_edits(plan, opts.force)
    report = BuildReport(project_dir=out, package=proj.package, lock_file=lf, plan=plan)
    if plan.nothing_to_do:
        return report

    for slug, reason in plan.dirty.items():
        echo(f"rebuilding does {slug}: {reason}")
    llm = llm_factory()
    dirty_blocks = [d for d in fe.program.does if d.slug in plan.dirty]
    if dirty_blocks:
        fe, answers = settle(path, fe, llm, dirty_blocks, yes=opts.yes, ask=opts.ask)
        for a in answers:
            echo(f"decided ({a.block}): {a.text}")
        proj = scaffold(fe)
    context = project_context(fe, proj.package, models_source(proj), proj.has_db)
    does_by_slug = {d.slug: d for d in fe.program.does}

    # Signatures and schedules of every block: regenerated ones from this build, the rest
    # from the lock (their code on disk is unchanged).
    signatures = {
        s: sig for s, sig in (lock.signatures() if lock else {}).items() if s in does_by_slug
    }
    schedules: dict[str, Schedule | None] = {}
    if lock:
        for key, entry in lock.does_blocks().items():
            sched = entry.schedule
            schedules[key.split(":", 1)[1]] = Schedule(**sched) if sched else None

    modules: dict[str, GeneratedModule] = {}
    for does in fe.program.does:
        if does.slug not in plan.dirty:
            continue
        echo(f"generating does {does.slug}")
        others = {s: sig for s, sig in signatures.items() if s != does.slug}
        m = generate_block(
            llm,
            fe,
            does,
            proj,
            others,
            context=context,
            previous_signature=signatures.get(does.slug),
        )
        modules[does.slug] = m
        signatures[does.slug] = m.signature
        schedules[does.slug] = m.schedule
        report.regenerated.append(does.slug)

    files = dict(proj.files)
    test_files: dict[str, list[str]] = {}
    for slug, m in modules.items():
        does = does_by_slug[slug]
        files[m.path] = proj.header(f"does {slug}") + m.source
        echo(f"translating examples and rules of {slug} into tests")
        test_files[slug] = []
        for kind, make in (("examples", example_tests), ("rules", rule_tests)):
            src = make(llm, fe, does, proj, m.signature, context=context)
            if src is not None:
                rel = proj.test_path(slug, kind)
                files[rel] = proj.header(f"{kind} of does {slug}") + src
                test_files[slug].append(rel)

    _remove_stale(out, lock, plan, proj, modules)
    write_files(out, files)

    # Glue needs every block's signature: regenerated ones in memory, the rest from disk.
    glue: list[BlockGlue] = []
    for does in fe.program.does:
        module = modules.get(does.slug)
        if module is None:
            source = strip_header((out / proj.module_path(does.slug)).read_text(encoding="utf-8"))
            info = function_info(ast.parse(source), does.slug)
            if info is None:  # pragma: no cover - the lock said this block was green
                raise LockError(f"the module of does {does.slug!r} lost its function")
            module = GeneratedModule(
                does.slug, proj.module_path(does.slug), source, info, schedules.get(does.slug), ""
            )
        glue.append(glue_for(module))
    glue_out = glue_files(fe, proj, glue)
    write_files(out, glue_out)

    all_tests = sorted(
        {rel for rels in test_files.values() for rel in rels}
        | {
            rel
            for key, entry in (lock.does_blocks().items() if lock else [])
            if key.split(":", 1)[1] in does_by_slug and key.split(":", 1)[1] not in modules
            for rel in entry.tests
        }
    )
    current_modules: dict[str, GeneratedModule | None] = {
        slug: modules.get(slug) for slug in does_by_slug
    }

    def repair(slug: str, attempt: int, failures: str) -> str:
        does = does_by_slug[slug]
        rel = proj.module_path(slug)
        current = current_modules.get(slug)
        if current is None:
            source = strip_header((out / rel).read_text(encoding="utf-8"))
            info = function_info(ast.parse(source), slug)
            if info is None:
                raise GenerationError(f"the module of does {slug!r} has no function {slug}")
            current = GeneratedModule(slug, rel, source, info, schedules.get(slug), "")
        others = {s: sig for s, sig in signatures.items() if s != slug}
        fixed = repair_block(
            llm, fe, does, proj, current, failures, others, context=context, attempt=attempt
        )
        current_modules[slug] = fixed
        return proj.header(f"does {slug}") + fixed.source

    echo("running the generated tests")
    report.loop = green_loop(
        out,
        test_files=all_tests,
        modules={s: proj.module_path(s) for s in does_by_slug},
        repair=repair,
        max_iters=opts.max_iters,
        echo=echo,
    )
    report.calls = dict(getattr(llm, "stats", {}))
    _write_lock(
        path,
        lf,
        out,
        fe,
        proj,
        lock,
        report,
        modules,
        test_files,
        schedules,
        signatures,
        glue_out,
        getattr(llm, "model", ""),
    )
    return report


def _remove_stale(
    out: Path,
    lock: Lock | None,
    plan: BuildPlan,
    proj: Project,
    modules: dict[str, GeneratedModule],
) -> None:
    """Delete generated files of removed blocks, and old test files of regenerated blocks."""
    if lock is None:
        return
    stale: set[str] = set()
    for key in plan.removed:
        stale |= set(lock.blocks[key].all_files())
    for slug in modules:
        entry = lock.blocks.get(_does_key(slug))
        if entry:
            stale |= set(entry.tests)  # a block may lose its rules or examples
    for rel in sorted(stale):
        path = out / rel
        if path.exists():
            path.chmod(0o644)
            path.unlink()


def _write_lock(
    path: Path,
    lf: Path,
    out: Path,
    fe: Frontend,
    proj: Project,
    old: Lock | None,
    report: BuildReport,
    modules: dict[str, GeneratedModule],
    test_files: dict[str, list[str]],
    schedules: dict[str, Schedule | None],
    signatures: dict[str, str],
    glue_out: dict[str, str],
    model: str,
) -> None:
    program = fe.program
    assert report.loop is not None
    loop = report.loop
    failing = loop.result.failing_blocks()
    if not loop.ok and not failing:
        # The suite is red but no block owns the failure (conftest, collection): every block
        # is suspect, so none may be skipped next time.
        failing = {d.slug for d in program.does}

    def h(rel: str) -> str:
        return content_hash((out / rel).read_text(encoding="utf-8"))

    lock = Lock(
        model=model or (old.model if old else ""),
        source=path.name,
        out_dir=relative_out_dir(out, lf),
        created_at=old.created_at if old else now_iso(),
    )
    lock.app_hash = node_hash(program.app)
    lock.shared_hash = shared_context_hash(program)
    lock.shared_files = {rel: h(rel) for rel in [*proj.files, *glue_out]}
    models_rel = f"src/{proj.package}/models.py"
    for t in program.things:
        lock.blocks[f"thing:{t.name}"] = BlockLock(
            kind="thing",
            source_hash=node_hash(t),
            input_hash=node_hash(t),
            ast=to_data(t, positions=False),
            files={models_rel: h(models_rel)},
        )
    for does in program.does:
        slug = does.slug
        key = _does_key(slug)
        prev = old.blocks.get(key) if old else None
        regenerated = slug in modules
        rel = proj.module_path(slug)
        tests = test_files[slug] if regenerated else list(prev.tests if prev else [])
        sched = schedules.get(slug)
        repaired = loop.iterations.get(slug, 0)
        status = "red" if slug in failing else "green"
        lock.blocks[key] = BlockLock(
            kind="does",
            source_hash=node_hash(does),
            input_hash=does_input_hash(program, does),
            ast=to_data(does, positions=False),
            files={rel: h(rel)},
            tests={t: loop.test_hashes.get(t) or h(t) for t in tests},
            signature=signatures.get(slug),
            schedule={"cron": sched.cron, "timezone": sched.timezone} if sched else None,
            status=status,
            iterations=repaired if (regenerated or repaired) else (prev.iterations if prev else 0),
            generated_at=(
                lock.updated_at if (regenerated or repaired) or prev is None else prev.generated_at
            ),
        )
    lock.save(lf)
