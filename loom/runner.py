"""Run the generated project's pytest suite and map results back to does blocks."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
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
        return RunResult(returncode=proc.returncode, output=proc.stdout + proc.stderr, cases=cases)
