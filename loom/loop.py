"""The green loop: run the tests, repair only the failing blocks, never touch a test.

Tests derived from examples and rules are the contract. Before the loop they are hashed and
made read-only; after every round the hashes are verified, and any change aborts the build with
:class:`~loom.errors.TestTamperError`. The repair callback can only return module source for
one block, which the loop writes to that block's module and nowhere else.
"""

from __future__ import annotations

import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from loom.errors import GenerationError, TestTamperError
from loom.lock import content_hash
from loom.runner import RunResult, run_pytest

# (block slug, repair round, failure report) -> the block's new module file content
RepairFn = Callable[[str, int, str], str]
Runner = Callable[[Path], RunResult]


@dataclass
class LoopOutcome:
    result: RunResult
    rounds: int
    iterations: dict[str, int] = field(default_factory=dict)  # repair rounds per block
    test_hashes: dict[str, str] = field(default_factory=dict)
    repair_errors: dict[str, list[str]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.result.ok


def hash_tests(root: Path, test_files: list[str]) -> dict[str, str]:
    return {rel: content_hash((root / rel).read_text(encoding="utf-8")) for rel in test_files}


def make_read_only(root: Path, test_files: list[str]) -> None:
    for rel in test_files:
        path = root / rel
        mode = path.stat().st_mode
        path.chmod(mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def verify_tests(root: Path, expected: dict[str, str]) -> None:
    changed = []
    for rel, digest in expected.items():
        path = root / rel
        if not path.exists() or content_hash(path.read_text(encoding="utf-8")) != digest:
            changed.append(rel)
    if changed:
        raise TestTamperError(
            "locked test files changed during the green loop: "
            + ", ".join(changed)
            + ". Tests derived from examples and rules are immutable; the build was aborted."
        )


def green_loop(
    root: Path,
    *,
    test_files: list[str],
    modules: dict[str, str],  # block slug -> module path (relative to root)
    repair: RepairFn,
    max_iters: int,
    echo: Callable[[str], None] = print,
    run: Runner = run_pytest,
) -> LoopOutcome:
    expected = hash_tests(root, test_files)
    make_read_only(root, test_files)
    result = run(root)
    verify_tests(root, expected)
    outcome = LoopOutcome(result=result, rounds=0, test_hashes=expected)

    while not result.ok and outcome.rounds < max_iters:
        failing = sorted(b for b in result.failing_blocks() if b in modules)
        if not failing:
            break  # failures that no block owns (conftest, collection): nothing to repair
        outcome.rounds += 1
        for slug in failing:
            echo(f"repairing does {slug} (round {outcome.rounds} of {max_iters})")
            try:
                source = repair(slug, outcome.rounds, result.failure_report(slug))
            except GenerationError as exc:
                outcome.repair_errors.setdefault(slug, []).append(str(exc))
                echo(f"  repair of {slug} was rejected: {str(exc).splitlines()[0]}")
                continue
            (root / modules[slug]).write_text(source, encoding="utf-8")
            outcome.iterations[slug] = outcome.iterations.get(slug, 0) + 1
        verify_tests(root, expected)
        result = run(root)
        verify_tests(root, expected)
        outcome.result = result
    return outcome
