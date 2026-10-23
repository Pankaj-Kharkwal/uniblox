"""LLM call #2: one Python module per does block, plus repairs from failing tests."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from loom.context import system_prompt
from loom.errors import GenerationError
from loom.frontend import Frontend
from loom.llm import LLM, LLMRequest
from loom.nodes import Does
from loom.render import render_does, render_exact
from loom.scaffold import BlockGlue, Project, Schedule
from loom.validate import FunctionInfo, check_module

CODEGEN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "module_source": {"type": "string"},
        "schedule": {
            "type": "object",
            "properties": {"cron": {"type": "string"}, "timezone": {"type": "string"}},
            "required": ["cron", "timezone"],
            "additionalProperties": False,
        },
        "notes": {"type": "string"},
    },
    "required": ["module_source", "schedule", "notes"],
    "additionalProperties": False,
}

REPAIR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"module_source": {"type": "string"}, "notes": {"type": "string"}},
    "required": ["module_source", "notes"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class GeneratedModule:
    slug: str
    path: str  # relative to the project root
    source: str  # without the generated-file header
    info: FunctionInfo
    schedule: Schedule | None
    notes: str

    @property
    def signature(self) -> str:
        return self.info.signature


def guard_ids(does: Does) -> list[str]:
    return [f"{does.slug}:{rid}" for rid, _ in does.rule_ids()]


def block_brief(
    fe: Frontend,
    does: Does,
    proj: Project,
    others: Mapping[str, str],
    previous_signature: str | None = None,
) -> str:
    """The per-block part of a codegen or repair prompt."""
    pkg = proj.package
    out = [
        f"Function name: `{does.slug}`",
        f"Module: `{pkg}.{does.slug}` (file {proj.module_path(does.slug)})",
        f"Target has a database: {'yes (sqlite via SQLModel)' if proj.has_db else 'no'}",
        "",
        "The does block (lines prefixed [L<n>], rules with their id):",
        "",
        render_does(does, numbered=True),
        "",
    ]
    if does.rules:
        out.append("Rule guard ids:")
        for (rid, rule), gid in zip(does.rule_ids(), guard_ids(does), strict=True):
            out.append(f'- {rid} -> guard id "{gid}": {rule.sentence}')
        out.append("")
    if does.exacts:
        out.append(f"Exact code of this block, importable from `{pkg}.{does.slug}_exact`:")
        out += [render_exact(e) for e in does.exacts]
        out.append("")
    if previous_signature:
        out.append(
            f"The previous version of this function had the signature `{previous_signature}`. "
            "Keep it unless the change to the block requires a different one."
        )
        out.append("")
    if others:
        out.append("Other functions already generated in this project (signatures only):")
        for slug, sig in sorted(others.items()):
            out.append(f"- `from {pkg}.{slug} import {slug}`: `{sig}`")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def _schedule(raw: Any) -> Schedule | None:
    if not isinstance(raw, dict):
        return None
    cron = " ".join(str(raw.get("cron", "")).split())
    tz = str(raw.get("timezone", "")).strip()
    if not cron:
        return None
    return Schedule(cron=cron, timezone=tz or "UTC")


def _module_from(
    out: dict[str, Any], does: Does, proj: Project, schedule: Schedule | None
) -> tuple[GeneratedModule | None, list[str]]:
    source = str(out.get("module_source", "")).strip("\n") + "\n"
    info, errors = check_module(
        source, slug=does.slug, rule_ids=guard_ids(does), package=proj.package
    )
    if errors or info is None:
        return None, errors
    module = GeneratedModule(
        slug=does.slug,
        path=proj.module_path(does.slug),
        source=source,
        info=info,
        schedule=schedule,
        notes=str(out.get("notes", "")),
    )
    return module, []


def _retry_prompt(prompt: str, errors: list[str], source: str) -> str:
    listing = "\n".join(f"- {e}" for e in errors)
    return (
        f"{prompt}\nYour previous module failed these checks:\n{listing}\n\n"
        f"Previous module:\n```python\n{source}```\n\nReturn a corrected module."
    )


def generate_block(
    llm: LLM,
    fe: Frontend,
    does: Does,
    proj: Project,
    others: Mapping[str, str],
    *,
    context: str,
    previous_signature: str | None = None,
) -> GeneratedModule:
    prompt = block_brief(fe, does, proj, others, previous_signature) + "\nWrite the module."
    request = LLMRequest(
        kind="codegen",
        label=f"does:{does.slug}",
        system=system_prompt("codegen"),
        context=context,
        prompt=prompt,
        schema=CODEGEN_SCHEMA,
        effort="medium",
    )
    out = llm.complete(request)
    module, errors = _module_from(out, does, proj, _schedule(out.get("schedule")))
    if module is None:
        retry = replace(
            request, prompt=_retry_prompt(prompt, errors, str(out.get("module_source", "")))
        )
        out = llm.complete(retry)
        module, errors = _module_from(out, does, proj, _schedule(out.get("schedule")))
    if module is None:
        raise GenerationError(
            f"generated code for does {does.slug!r} failed validation:\n"
            + "\n".join(f"  - {e}" for e in errors)
        )
    if does.trigger is not None and module.schedule is None:
        raise GenerationError(
            f"does {does.slug!r} has an `every` trigger but no schedule came back"
        )
    return module


def repair_block(
    llm: LLM,
    fe: Frontend,
    does: Does,
    proj: Project,
    current: GeneratedModule,
    failures: str,
    others: Mapping[str, str],
    *,
    context: str,
    attempt: int,
) -> GeneratedModule:
    """Regenerate one block's module from its failing test output. Tests are not sent back."""
    prompt = (
        block_brief(fe, does, proj, others)
        + f"\nCurrent module ({current.path}):\n```python\n{current.source}```\n\n"
        + f"Failing tests (pytest output, repair attempt {attempt}):\n```\n{failures}\n```\n\n"
        + "Fix the module."
    )
    request = LLMRequest(
        kind="repair",
        label=f"does:{does.slug}",
        system=system_prompt("repair"),
        context=context,
        prompt=prompt,
        schema=REPAIR_SCHEMA,
        effort="medium",
    )
    out = llm.complete(request)
    module, errors = _module_from(out, does, proj, current.schedule)
    if module is not None and module.signature != current.signature:
        errors = [f"the signature changed: it must stay `{current.signature}`"]
        module = None
    if module is None:
        raise GenerationError(
            f"repair of does {does.slug!r} failed validation:\n"
            + "\n".join(f"  - {e}" for e in errors)
        )
    return module


def glue_for(module: GeneratedModule) -> BlockGlue:
    info = module.info
    takes_session = bool(info.params) and info.params[0] == "session"
    takes_now = "now" in info.params
    extra_required = [p for p in info.required if p not in ("session", "now")]
    return BlockGlue(
        slug=module.slug,
        takes_session=takes_session,
        takes_now=takes_now,
        callable_without_args=not extra_required,
        schedule=module.schedule,
    )
