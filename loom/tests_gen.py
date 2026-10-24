"""Examples and rules -> pytest modules (LLM translation, structurally validated).

The test writer sees the function's signature, never its body. Output is accepted only if
every example has a test that calls the function and checks every ``expect`` clause, and every
rule has a test that calls the function and asserts.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from loom.context import system_prompt
from loom.errors import GenerationError
from loom.frontend import Frontend
from loom.llm import LLM, LLMRequest
from loom.nodes import Does
from loom.render import render_does
from loom.scaffold import Project
from loom.validate import check_example_tests, check_rule_tests

EXAMPLE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "module_source": {"type": "string"},
        "tests": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "example": {"type": "string"},
                    "function": {"type": "string"},
                    "clauses": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "clause": {"type": "string"},
                                "check": {"type": "string"},
                            },
                            "required": ["clause", "check"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["example", "function", "clauses"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["module_source", "tests"],
    "additionalProperties": False,
}

RULE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "module_source": {"type": "string"},
        "tests": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"rule": {"type": "string"}, "function": {"type": "string"}},
                "required": ["rule", "function"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["module_source", "tests"],
    "additionalProperties": False,
}


def clause_ids(does: Does) -> dict[str, list[str]]:
    return {
        f"E{n}": [f"E{n}.{c}" for c in range(1, len(ex.expect) + 1)]
        for n, ex in enumerate(does.examples, start=1)
    }


def _header(does: Does, proj: Project, signature: str) -> list[str]:
    pkg = proj.package
    return [
        f"Function under test: `from {pkg}.{does.slug} import {does.slug}`",
        f"Signature: `{signature}`",
        f"Target has a database: {'yes (use the `session` fixture)' if proj.has_db else 'no'}",
        "",
        "The does block it implements:",
        "",
        render_does(does, numbered=True, examples=False),
        "",
    ]


def _examples_prompt(does: Does, proj: Project, signature: str) -> str:
    out = _header(does, proj, signature)
    out.append("Examples to translate, with numbered expect clauses:")
    for n, ex in enumerate(does.examples, start=1):
        out += ["", f"E{n}", f"  given {ex.given}"]
        out += [f"  E{n}.{c}: expect {clause}" for c, clause in enumerate(ex.expect, start=1)]
    return "\n".join(out) + "\n\nWrite the test module.\n"


def _rules_prompt(does: Does, proj: Project, signature: str) -> str:
    out = _header(does, proj, signature)
    out.append("Rules to test:")
    for rid, rule in does.rule_ids():
        out.append(f"- {rid} (guard id {does.slug}:{rid}): {rule.sentence}")
    return "\n".join(out) + "\n\nWrite the test module.\n"


def _run(llm: LLM, request: LLMRequest, check: Any, what: str) -> str:
    out = llm.complete(request)
    source = str(out.get("module_source", "")).strip("\n") + "\n"
    errors: list[str] = check(source, out.get("tests") or [])
    if errors:
        listing = "\n".join(f"- {e}" for e in errors)
        retry = replace(
            request,
            prompt=(
                f"{request.prompt}\nYour previous answer failed these checks:\n{listing}\n\n"
                f"Previous module:\n```python\n{source}```\n\nReturn a corrected answer."
            ),
        )
        out = llm.complete(retry)
        source = str(out.get("module_source", "")).strip("\n") + "\n"
        errors = check(source, out.get("tests") or [])
    if errors:
        raise GenerationError(
            f"{what} failed validation:\n" + "\n".join(f"  - {e}" for e in errors)
        )
    return source


def _with_broken(prompt: str, broken: tuple[str, str] | None) -> str:
    """Add the previous module and the error it hit before reaching the function under test."""
    if broken is None:
        return prompt
    source, error = broken
    return (
        f"{prompt}\nYour previous test module could not run: it failed before reaching the "
        f"function under test, so the test itself is broken.\n\nError:\n```\n{error}\n```\n\n"
        f"Previous module:\n```python\n{source}```\n\nReturn a corrected answer.\n"
    )


def example_tests(
    llm: LLM,
    fe: Frontend,
    does: Does,
    proj: Project,
    signature: str,
    *,
    context: str,
    broken: tuple[str, str] | None = None,
) -> str | None:
    """The example test module for a block, or None when it has no examples.

    ``broken`` is a previous attempt (source, error) whose tests could not even run.
    """
    if not does.examples:
        return None
    ids = clause_ids(does)
    request = LLMRequest(
        kind="example_tests",
        label=f"does:{does.slug}",
        system=system_prompt("example_tests"),
        context=context,
        prompt=_with_broken(_examples_prompt(does, proj, signature), broken),
        schema=EXAMPLE_SCHEMA,
        effort="low",
    )

    def check(source: str, mapping: list[dict[str, object]]) -> list[str]:
        return check_example_tests(
            source, slug=does.slug, package=proj.package, examples=ids, mapping=mapping
        )

    return _run(llm, request, check, f"example tests for does {does.slug!r}")


def rule_tests(
    llm: LLM,
    fe: Frontend,
    does: Does,
    proj: Project,
    signature: str,
    *,
    context: str,
    broken: tuple[str, str] | None = None,
) -> str | None:
    """The rule test module for a block, or None when it has no rules."""
    if not does.rules:
        return None
    rids = [rid for rid, _ in does.rule_ids()]
    request = LLMRequest(
        kind="rule_tests",
        label=f"does:{does.slug}",
        system=system_prompt("rule_tests"),
        context=context,
        prompt=_with_broken(_rules_prompt(does, proj, signature), broken),
        schema=RULE_SCHEMA,
        effort="low",
    )

    def check(source: str, mapping: list[dict[str, object]]) -> list[str]:
        return check_rule_tests(
            source, slug=does.slug, package=proj.package, rule_ids=rids, mapping=mapping
        )

    return _run(llm, request, check, f"rule tests for does {does.slug!r}")
