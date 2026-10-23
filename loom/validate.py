"""Structural checks on model output, done with the stdlib ``ast`` module.

The model's code is never trusted to have followed instructions: a module must define the
block's function and guard every rule; an example test must call the function and assert every
``expect`` clause; nothing may import Loom or reach for the filesystem, network or processes.
"""

from __future__ import annotations

import ast
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass

DENIED = frozenset(
    {
        "os",
        "sys",
        "subprocess",
        "socket",
        "shutil",
        "pathlib",
        "importlib",
        "ctypes",
        "multiprocessing",
        "threading",
        "urllib",
        "http",
        "ftplib",
        "smtplib",
        "pickle",
        "builtins",
        "loom",
    }
)
THIRD_PARTY = frozenset({"pydantic", "sqlmodel", "sqlalchemy", "fastapi", "apscheduler"})
TEST_ONLY = frozenset({"pytest", "hypothesis"})
_LOOM_WORD = re.compile(r"\bloom\b", re.IGNORECASE)


@dataclass(frozen=True)
class FunctionInfo:
    name: str
    signature: str
    params: tuple[str, ...]
    required: tuple[str, ...]  # parameters without defaults
    kwonly: tuple[str, ...]


def _parse(source: str, what: str) -> tuple[ast.Module | None, list[str]]:
    try:
        return ast.parse(source), []
    except SyntaxError as exc:
        return None, [f"{what} is not valid Python: line {exc.lineno}: {exc.msg}"]


def _import_errors(tree: ast.Module, package: str, *, tests: bool) -> list[str]:
    allowed_third = THIRD_PARTY | (TEST_ONLY if tests else frozenset())
    errors = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                errors.append("use absolute imports (from <package>.<module> import ...)")
                continue
            names = [node.module or ""]
        for name in names:
            top = name.split(".")[0]
            if top in DENIED:
                errors.append(f"import of {name!r} is not allowed in generated code")
            elif top == package or top in allowed_third:
                continue
            elif top not in sys.stdlib_module_names:
                errors.append(f"import of {name!r} is not available in the generated project")
    return errors


def _common(
    source: str, package: str, *, tests: bool, what: str
) -> tuple[ast.Module | None, list[str]]:
    tree, errors = _parse(source, what)
    if tree is None:
        return None, errors
    errors += _import_errors(tree, package, tests=tests)
    if _LOOM_WORD.search(source):
        errors.append(f"{what} must not mention Loom: the generated project stands on its own")
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"open", "exec", "eval", "__import__", "compile"}
        ):
            errors.append(f"{what} must not call {node.func.id}()")
    return tree, errors


def function_info(tree: ast.Module, name: str) -> FunctionInfo | None:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            a = node.args
            positional = [*a.posonlyargs, *a.args]
            n_defaults = len(a.defaults)
            required = [p.arg for p in positional[: len(positional) - n_defaults]]
            required += [
                k.arg for k, d in zip(a.kwonlyargs, a.kw_defaults, strict=True) if d is None
            ]
            sig = f"def {name}({ast.unparse(a)})"
            if node.returns is not None:
                sig += f" -> {ast.unparse(node.returns)}"
            return FunctionInfo(
                name=name,
                signature=sig,
                params=tuple(p.arg for p in [*positional, *a.kwonlyargs]),
                required=tuple(required),
                kwonly=tuple(k.arg for k in a.kwonlyargs),
            )
    return None


def _guard_ids(tree: ast.AST) -> set[str]:
    ids: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        fname = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if fname != "guard":
            continue
        candidates = list(node.args[1:2]) + [k.value for k in node.keywords if k.arg == "rule_id"]
        for c in candidates:
            if isinstance(c, ast.Constant) and isinstance(c.value, str):
                ids.add(c.value)
    return ids


def check_module(
    source: str, *, slug: str, rule_ids: Iterable[str], package: str
) -> tuple[FunctionInfo | None, list[str]]:
    """Validate a generated does-block module. Returns its function info and any errors."""
    tree, errors = _common(source, package, tests=False, what="the module")
    if tree is None:
        return None, errors
    info = function_info(tree, slug)
    if info is None:
        errors.append(f"the module must define a top-level function named {slug!r}")
    guarded = _guard_ids(tree)
    for rid in rule_ids:
        if rid not in guarded:
            errors.append(f'rule {rid} has no runtime guard: call guard(<condition>, "{rid}")')
    return info, errors


def _calls(tree: ast.AST, name: str) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if (isinstance(f, ast.Name) and f.id == name) or (
                isinstance(f, ast.Attribute) and f.attr == name
            ):
                return True
    return False


def _test_functions(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    return {
        n.name: n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name.startswith("test")
    }


def _is_check(node: ast.AST) -> bool:
    """An assert statement, or a ``pytest.raises(...)`` call."""
    if isinstance(node, ast.Assert):
        return True
    if isinstance(node, ast.Call):
        f = node.func
        return isinstance(f, ast.Attribute) and f.attr == "raises"
    return False


def _snippet_present(snippet: str, fn: ast.FunctionDef) -> bool:
    """Is ``snippet`` (one assert, or a pytest.raises call) structurally present in ``fn``?"""
    text = snippet.strip().rstrip(":")
    if text.startswith("with "):
        text = text[len("with ") :].split(" as ")[0]
    try:
        parsed = ast.parse(text)
    except SyntaxError:
        return False
    if len(parsed.body) != 1:
        return False
    target: ast.AST = parsed.body[0]
    if isinstance(target, ast.Expr):
        target = target.value
    if not _is_check(target):
        return False
    want = ast.dump(target)
    return any(ast.dump(n) == want for n in ast.walk(fn))


def check_example_tests(
    source: str,
    *,
    slug: str,
    package: str,
    examples: dict[str, list[str]],  # "E1" -> ["E1.1", "E1.2"]
    mapping: list[dict[str, object]],
) -> list[str]:
    tree, errors = _common(source, package, tests=True, what="the example test module")
    if tree is None:
        return errors
    fns = _test_functions(tree)
    by_example: dict[str, dict[str, object]] = {}
    for item in mapping:
        by_example[str(item.get("example"))] = item
    for ex_id, clause_ids in examples.items():
        found = by_example.get(ex_id)
        if found is None:
            errors.append(f"example {ex_id} has no test")
            continue
        item = found
        fname = str(item.get("function"))
        fn = fns.get(fname)
        if fn is None:
            errors.append(f"example {ex_id}: test function {fname!r} is not in the module")
            continue
        if not _calls(fn, slug):
            errors.append(f"example {ex_id}: {fname} never calls {slug}()")
        clauses = item.get("clauses")
        evidence: dict[str, list[str]] = {}
        for c in clauses if isinstance(clauses, list) else []:
            if isinstance(c, dict):
                evidence.setdefault(str(c.get("clause")), []).append(str(c.get("check", "")))
        for cid in clause_ids:
            checks = evidence.get(cid, [])
            if not checks:
                errors.append(f"expect clause {cid} has no assert")
            elif not any(_snippet_present(s, fn) for s in checks):
                errors.append(
                    f"expect clause {cid}: the reported check is not an assert (or "
                    f"pytest.raises) that appears in {fname}"
                )
    return errors


def check_rule_tests(
    source: str,
    *,
    slug: str,
    package: str,
    rule_ids: list[str],
    mapping: list[dict[str, object]],
) -> list[str]:
    tree, errors = _common(source, package, tests=True, what="the rule test module")
    if tree is None:
        return errors
    fns = _test_functions(tree)
    covered: set[str] = set()
    for item in mapping:
        rid = str(item.get("rule"))
        fname = str(item.get("function"))
        fn = fns.get(fname)
        if fn is None:
            errors.append(f"rule {rid}: test function {fname!r} is not in the module")
            continue
        if not _calls(fn, slug):
            errors.append(f"rule {rid}: {fname} never calls {slug}()")
            continue
        if not any(_is_check(n) for n in ast.walk(fn)):
            errors.append(f"rule {rid}: {fname} has no assert")
            continue
        covered.add(rid)
    for rid in rule_ids:
        if rid not in covered and not any(e.startswith(f"rule {rid}") for e in errors):
            errors.append(f"rule {rid} has no test")
    return errors
