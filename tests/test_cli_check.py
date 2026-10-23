from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from loom import __version__
from loom.cli import app

ROOT = Path(__file__).resolve().parent.parent
runner = CliRunner()


def test_check_ok_on_both_fixtures() -> None:
    res = runner.invoke(app, ["check", str(ROOT / "examples" / "todo.loom"), "--no-scan"])
    assert res.exit_code == 0, res.output
    assert "app TodoList: 2 things, 2 does blocks, 3 rules, 4 examples" in res.output
    res = runner.invoke(app, ["check", str(ROOT / "examples" / "invoice_chaser.loom"), "--no-scan"])
    assert res.exit_code == 0, res.output
    assert "1 things, 1 does blocks, 2 rules, 3 examples" in res.output


def test_check_reports_errors_and_exits_1(tmp_path: Path) -> None:
    bad = tmp_path / "bad.loom"
    bad.write_text("app X\ntarget: python 3.12\nthing T\n  amount: monee (INR)\n")
    res = runner.invoke(app, ["check", str(bad), "--no-scan"])
    assert res.exit_code == 1
    assert "bad.loom:4:11: error: unknown type 'monee'" in res.output
    assert "hint: did you mean 'money'?" in res.output


def test_version() -> None:
    res = runner.invoke(app, ["version"])
    assert res.output.strip() == __version__
