"""``loom diff``: what changed in intent since the last build, and what will regenerate.

Pure: compares the current AST with the normalized AST stored in the lock. No model calls.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loom.build import BuildPlan, default_out_dir, plan_build
from loom.frontend import Frontend
from loom.lock import Lock
from loom.nodes import App, Does, Field, Step, Thing, from_data
from loom.render import render_app, render_field, render_line
from loom.scaffold import scaffold


@dataclass
class IntentChange:
    block: str  # "does chase overdue invoices", "thing Invoice", "app", "exact"
    sentence: str


@dataclass
class DiffReport:
    changes: list[IntentChange] = field(default_factory=list)
    plan: BuildPlan | None = None
    files: dict[str, list[str]] = field(default_factory=dict)  # does slug -> files rewritten
    package: str = ""
    has_lock: bool = True

    @property
    def empty(self) -> bool:
        plan = self.plan
        return not self.changes and (plan is None or (plan.nothing_to_do and not plan.hand_edited))


def _q(text: str) -> str:
    return f'"{text}"'


def _thing_changes(old: Thing | None, new: Thing | None) -> list[str]:
    if old is None and new is not None:
        return [f"Added thing {new.name} with fields " + ", ".join(f.name for f in new.fields)]
    if new is None and old is not None:
        return [f"Removed thing {old.name}"]
    assert old is not None and new is not None
    out = []
    before: dict[str, Field] = {f.name: f for f in old.fields}
    after: dict[str, Field] = {f.name: f for f in new.fields}
    for name, f in after.items():
        if name not in before:
            out.append(f"Added field {new.name}.{name}: {render_field(f).split(': ', 1)[1]}")
        elif before[name] != f:
            was = render_field(before[name]).split(": ", 1)[1]
            now = render_field(f).split(": ", 1)[1]
            out.append(f"Changed field {new.name}.{name}: {was} → {now}")
    out += [f"Removed field {old.name}.{name}" for name in before if name not in after]
    return out


def _step_key(line: Any) -> str:
    return render_line(line)


def _does_changes(old: Does | None, new: Does | None) -> list[str]:
    if old is None and new is not None:
        return [f"Added does block: {new.description}"]
    if new is None and old is not None:
        return [f"Removed does block: {old.description}"]
    assert old is not None and new is not None
    out: list[str] = []

    # schedule
    ot, nt = old.trigger, new.trigger
    if ot is None and nt is not None:
        out.append(f"Added schedule: every {nt.text}")
    elif ot is not None and nt is None:
        out.append(f"Removed schedule: every {ot.text}")
    elif ot is not None and nt is not None and ot.text != nt.text:
        out.append(f"Changed schedule: every {ot.text} → every {nt.text}")

    # rules (matched by text: a reworded rule is a removal plus an addition)
    old_rules = [r.sentence for r in old.rules]
    new_rules = [r.sentence for r in new.rules]
    out += [f"Added rule: {r}" for r in new_rules if r not in old_rules]
    out += [f"Removed rule: {r}" for r in old_rules if r not in new_rules]

    # steps, in order
    old_steps = [s for s in old.lines if isinstance(s, Step)]
    new_steps = [s for s in new.lines if isinstance(s, Step)]
    sm = difflib.SequenceMatcher(a=[s.text for s in old_steps], b=[s.text for s in new_steps])
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "insert":
            out += [f"Added step: {_q(_step_key(s))}" for s in new_steps[j1:j2]]
        elif op == "delete":
            out += [f"Removed step: {_q(_step_key(s))}" for s in old_steps[i1:i2]]
        elif op == "replace":
            pairs = list(zip(old_steps[i1:i2], new_steps[j1:j2], strict=False))
            for k, (a, b) in enumerate(pairs):
                out.append(f"Changed step {j1 + k + 1}: {_q(a.text)} → {_q(b.text)}")
            out += [f"Removed step: {_q(_step_key(s))}" for s in old_steps[i1 + len(pairs) : i2]]
            out += [f"Added step: {_q(_step_key(s))}" for s in new_steps[j1 + len(pairs) : j2]]
        else:
            for a, b in zip(old_steps[i1:i2], new_steps[j1:j2], strict=True):
                if a.soft != b.soft:
                    was = a.soft.text if a.soft else "(none)"
                    now = b.soft.text if b.soft else "(none)"
                    out.append(f"Changed soft preference on {_q(b.text)}: {was} → {now}")

    # decisions
    old_dec = [d.text for d in old.all_decided()]
    new_dec = [d.text for d in new.all_decided()]
    out += [f"Recorded decision: {d}" for d in new_dec if d not in old_dec]
    out += [f"Removed decision: {d}" for d in old_dec if d not in new_dec]

    # exact code inside the block
    if [e.code for e in old.exacts] != [e.code for e in new.exacts]:
        out.append("Changed exact code")

    # examples, by position
    for n in range(max(len(old.examples), len(new.examples))):
        ea = old.examples[n] if n < len(old.examples) else None
        eb = new.examples[n] if n < len(new.examples) else None
        if ea is None and eb is not None:
            out.append(f"Added example {n + 1}: given {eb.given}; expect {', '.join(eb.expect)}")
        elif eb is None and ea is not None:
            out.append(f"Removed example {n + 1}: given {ea.given}")
        elif ea is not None and eb is not None and ea != eb:
            if ea.given != eb.given:
                out.append(f"Changed example {n + 1} given: {_q(ea.given)} → {_q(eb.given)}")
            if ea.expect != eb.expect:
                out.append(
                    f"Changed example {n + 1} expect: {_q(', '.join(ea.expect))} → "
                    f"{_q(', '.join(eb.expect))}"
                )
    return out


def intent_changes(fe: Frontend, lock: Lock) -> list[IntentChange]:
    program = fe.program
    changes: list[IntentChange] = []

    old_app = lock.blocks.get("app")
    if old_app is not None:
        was: App = from_data(old_app.ast)
        if was.name != program.app.name:
            changes.append(IntentChange("app", f"Renamed app: {was.name} → {program.app.name}"))
        if was.target != program.app.target:
            before = render_app(was).split("\n")[1]
            after = render_app(program.app).split("\n")[1]
            changes.append(
                IntentChange("app", f"Changed {before} → {after.removeprefix('target: ')}")
            )
    old_exact = lock.blocks.get("exact")
    old_codes = [e.code for e in from_data(old_exact.ast)] if old_exact else []
    if old_codes != [e.code for e in program.exacts]:
        changes.append(IntentChange("exact", "Changed top-level exact code"))

    olds = {k: from_data(v.ast) for k, v in lock.blocks.items() if v.kind in ("thing", "does")}
    news: dict[str, Thing | Does] = {f"thing:{t.name}": t for t in program.things}
    news.update({f"does:{d.slug}": d for d in program.does})
    for key in [*news, *(k for k in olds if k not in news)]:
        old, new = olds.get(key), news.get(key)
        if old == new:
            continue
        node = new or old
        if isinstance(node, Thing):
            label = f"thing {node.name}"
            sentences = _thing_changes(old, new)  # type: ignore[arg-type]
        else:
            assert isinstance(node, Does)
            label = f"does {node.description}"
            sentences = _does_changes(old, new)  # type: ignore[arg-type]
        changes += [IntentChange(label, s) for s in sentences]
    return changes


def diff(fe: Frontend, lock: Lock | None, lock_file: Path, out: Path | None = None) -> DiffReport:
    proj = scaffold(fe)
    if lock is None:
        plan = plan_build(fe, proj, None, out or default_out_dir(proj), force=False)
        report = DiffReport(plan=plan, package=proj.package, has_lock=False)
    else:
        target = out or lock.resolve_out_dir(lock_file)
        plan = plan_build(fe, proj, lock, target, force=False)
        report = DiffReport(changes=intent_changes(fe, lock), plan=plan, package=proj.package)
    for slug in plan.dirty:
        does = fe.program.does_block(slug)
        files = [proj.module_path(slug)]
        if does is not None and does.examples:
            files.append(proj.test_path(slug, "examples"))
        if does is not None and does.rules:
            files.append(proj.test_path(slug, "rules"))
        report.files[slug] = files
    return report
