"""Run the generated project's pytest suite and map results back to does blocks."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace
from pathlib import Path

_TEST_FILE = re.compile(r"^tests/test_(?P<slug>.+)_(?:examples|rules)\.py$")


@dataclass(frozen=True)
class CaseResult:
    file: str  # e.g. "tests/test_complete_task_examples.py"
    name: str
    outcome: str  # passed | failed | error | skipped
    message: str = ""
    details: str = ""

    @property
    def block(self) -> str | None:
        m = _TEST_FILE.match(self.file)
        return m["slug"] if m else None

    @property
    def broken_test(self) -> bool:
        """An error the test hits before reaching the code under test (a defect in the test).

        pytest reports setup and collection problems as errors, not failures. Missing
        fixtures, or errors whose traceback never leaves the tests/ folder, are the test's
        fault; an import error inside src/ is the code's.
        """
        if self.outcome != "error":
            return False
        text = f"{self.message}\n{self.details}"
        if "fixture '" in text and "not found" in text:
            return True
        return "tests/" in text and "src/" not in text


@dataclass
class RunResult:
    returncode: int
    output: str
    cases: list[CaseResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.failures

    @property
    def failures(self) -> list[CaseResult]:
        return [c for c in self.cases if c.outcome in ("failed", "error")]

    def failing_blocks(self) -> set[str]:
        return {c.block for c in self.failures if c.block}

    def repairable_blocks(self) -> set[str]:
        """Blocks with failures that a change to the code could fix."""
        return {c.block for c in self.failures if c.block and not c.broken_test}

    def broken_tests(self) -> dict[str, str]:
        """Test files whose tests cannot run, with their error output."""
        out: dict[str, str] = {}
        for c in self.failures:
            if c.broken_test:
                text = f"{c.name}: {c.message}\n{c.details}".strip()
                out[c.file] = (out.get(c.file, "") + "\n\n" + text).strip()[:6000]
        return out

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for c in self.cases:
            out[c.outcome] = out.get(c.outcome, 0) + 1
        return out

    def failure_report(self, slug: str, limit: int = 6000) -> str:
        """Trimmed failure details for one block, to feed back to the model."""
        parts = []
        for c in self.failures:
            if c.block == slug:
                parts.append(f"{c.file}::{c.name} {c.outcome.upper()}\n{c.details or c.message}")
        text = "\n\n".join(parts) or self.output[-limit:]
        return text if len(text) <= limit else text[:limit] + "\n... (truncated)"


def _file_of(case: ET.Element) -> str:
    file = case.get("file")
    if file:
        return file.replace("\\", "/")
    classname = case.get("classname", "")
    parts = classname.split(".")
    # "tests.test_x" or "tests.test_x.TestClass"
    for i in range(len(parts), 0, -1):
        if parts[i - 1].startswith("test_"):
            return "/".join(parts[:i]) + ".py"
    return classname.replace(".", "/") + ".py"


def parse_junit(xml_text: str) -> list[CaseResult]:
    root = ET.fromstring(xml_text)
    out = []
    for case in root.iter("testcase"):
        outcome, message, details = "passed", "", ""
        for tag in ("failure", "error", "skipped"):
            el = case.find(tag)
            if el is not None:
                outcome = {"failure": "failed", "error": "error", "skipped": "skipped"}[tag]
                message = el.get("message", "")
                details = (el.text or "").strip()
                break
        out.append(
            CaseResult(
                file=_file_of(case),
                name=case.get("name", ""),
                outcome=outcome,
                message=message,
                details=details,
            )
        )
    return out


def run_pytest(project: Path, *, timeout: int = 600, extra: list[str] | None = None) -> RunResult:
    """Run ``pytest`` in ``project`` with the current interpreter (src/ on the path)."""
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "report.xml"
        env = {**os.environ, "PYTHONPATH": str(project / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
        env.pop("PYTEST_ADDOPTS", None)
        cmd = [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-o",
            "junit_family=xunit1",
            f"--junitxml={report}",
            *(extra or []),
        ]
        try:
            proc = subprocess.run(
                cmd, cwd=project, env=env, capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired as exc:
            out = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            return RunResult(returncode=124, output=out + f"\npytest timed out after {timeout}s")
        cases = parse_junit(report.read_text()) if report.exists() else []
        # Paths relative to the project, so the output (fed back into prompts, hence into
        # cache keys) is the same on every machine.
        prefix = f"{project.resolve()}/"
        cases = [
            replace(c, message=c.message.replace(prefix, ""), details=c.details.replace(prefix, ""))
            for c in cases
        ]
        output = (proc.stdout + proc.stderr).replace(prefix, "")
        return RunResult(returncode=proc.returncode, output=output, cases=cases)
