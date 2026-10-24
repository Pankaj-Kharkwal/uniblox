"""The brief's acceptance criteria, replayed offline from REAL Claude responses.

tests/fixtures/llm holds responses recorded through Claude Code (`--backend claude-code`,
LOOM_RECORD=1). LOOM_MOCK=1 (set by conftest) replays them by prompt hash, so these tests
exercise exactly what the model wrote. They assert outcomes, not wording, so they survive a
re-recording.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from loom.cli import app
from loom.eject import loom_references
from loom.lock import Lock

ROOT = Path(__file__).resolve().parent.parent
RECORDED = ROOT / "tests" / "fixtures" / "llm"
runner = CliRunner()


def recorded_sources() -> set[str]:
    return {json.loads(p.read_text())["source"] for p in RECORDED.glob("*.json")}


def copy_example(tmp_path: Path, name: str) -> Path:
    dst = tmp_path / f"{name}.loom"
    shutil.copy(ROOT / "examples" / f"{name}.loom", dst)
    return dst


def test_fixtures_are_real_recordings() -> None:
    assert recorded_sources() == {"claude-code"}


def test_todo_green_locked_then_nothing_to_rebuild(tmp_path: Path) -> None:
    src = copy_example(tmp_path, "todo")
    res = runner.invoke(app, ["build", str(src), "--yes", "--out", str(tmp_path / "out")])
    assert res.exit_code == 0, res.output
    assert re.search(r"GREEN: \d+ passed", res.output)
    assert "# decided:" in src.read_text()  # real ambiguity answers were written back
    lock = Lock.load(tmp_path / "todo.loom.lock")
    assert lock is not None
    assert {b.status for b in lock.does_blocks().values()} == {"green"}

    again = runner.invoke(app, ["build", str(src), "--yes"])
    assert again.exit_code == 0
    assert again.output.strip() == "nothing to rebuild: todo.loom matches todo.loom.lock"


def test_invoice_green_with_24h_guard_and_test_then_eject(tmp_path: Path) -> None:
    src = copy_example(tmp_path, "invoice_chaser")
    out = tmp_path / "out"
    res = runner.invoke(app, ["build", str(src), "--yes", "--out", str(out)])
    assert res.exit_code == 0, res.output
    assert re.search(r"GREEN: \d+ passed", res.output)

    pkg = out / "src" / "invoice_chaser"
    module = (pkg / "chase_overdue_invoices.py").read_text()
    assert re.search(r'guard\([^)]*"chase_overdue_invoices:R1"', module, re.S)
    rules = (out / "tests" / "test_chase_overdue_invoices_rules.py").read_text()
    assert re.search(r"def test_r1_", rules)
    assert "queue_email(" in module
    assert "Asia/Kolkata" in (pkg / "scheduler.py").read_text()

    ejected = runner.invoke(app, ["eject", str(src), "--yes"])
    assert ejected.exit_code == 0, ejected.output
    assert "No Loom references remain." in ejected.output
    assert re.search(r"pytest: \d+ passed", ejected.output)
    assert loom_references(out) == []


@pytest.mark.parametrize("name", ["todo", "invoice_chaser"])
def test_replay_makes_no_network_calls(tmp_path: Path, name: str) -> None:
    from loom.llm import make_client

    src = copy_example(tmp_path, name)
    client = make_client()
    assert client.mock
    from loom.build import BuildOptions, build

    report = build(
        src, lambda: client, BuildOptions(out_dir=tmp_path / "out", yes=True, echo=lambda s: None)
    )
    assert report.ok
    assert client.stats["api_calls"] == 0 and client.stats["fixture_hits"] > 0
