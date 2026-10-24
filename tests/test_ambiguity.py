from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fakes import FakeLLM
from typer.testing import CliRunner

from loom.ambiguity import Ambiguity, request_for, resolve, scan_block, settle
from loom.cli import app
from loom.errors import UnresolvedAmbiguityError
from loom.frontend import load, load_text
from loom.llm import LLMRequest

ROOT = Path(__file__).resolve().parent.parent
runner = CliRunner()

SRC = """app X
target: python 3.12

does chase invoices
  every weekday at 10:00 IST
  find overdue invoices
  never email twice in 24h

example
  given an invoice due yesterday
  expect 1 email queued
"""


def canned(request: LLMRequest) -> dict[str, object]:
    return {
        "ambiguities": [
            {
                "line": 0,
                "question": "Does weekday include Saturday?",
                "options": ["weekdays are Monday to Friday", "weekdays are Monday to Saturday"],
                "suggested": 0,
                "why": "5 or 6 runs a week",
            },
            {
                "line": 99,  # out of range: becomes block level
                "question": "Which zone?",
                "options": ["  all   times are IST "],
                "suggested": 7,  # out of range: becomes 0
                "why": "",
            },
            {"line": 1, "question": "empty", "options": [], "suggested": 0, "why": ""},
        ]
    }


def test_request_is_numbered_cached_and_formatting_independent() -> None:
    fe = load_text(SRC)
    r = request_for(fe, fe.program.does[0])
    assert "[L0] every weekday at 10:00 IST" in r.prompt
    assert "[L2] (R1) never email twice in 24h" in r.prompt
    assert "# E1" in r.prompt
    assert r.kind == "ambiguity" and r.effort == "low"
    assert "Loom" in r.system and "app X" in r.context
    reformatted = load_text(SRC.replace("  ", "    ").replace("\n\n", "\n\n\n# note\n"))
    assert request_for(reformatted, reformatted.program.does[0]).key("m") == r.key("m")


def test_scan_normalizes_model_output() -> None:
    fe = load_text(SRC)
    ambs = scan_block(FakeLLM(canned), fe, fe.program.does[0])
    assert len(ambs) == 2  # the one without options is dropped
    assert ambs[0].suggestion == "weekdays are Monday to Friday"
    assert (ambs[1].line, ambs[1].suggested, ambs[1].suggestion) == (-1, 0, "all times are IST")


def test_resolve_with_yes_ask_and_unresolved() -> None:
    fe = load_text(SRC)
    ambs = scan_block(FakeLLM(canned), fe, fe.program.does[0])
    assert [a.text for a in resolve(ambs, fe, yes=True)] == [
        "weekdays are Monday to Friday",
        "all times are IST",
    ]

    def ask(amb: Ambiguity, does: object) -> str | None:
        return amb.options[-1] if amb.line == 0 else None

    with pytest.raises(UnresolvedAmbiguityError) as info:
        resolve(ambs, fe, ask=ask)
    assert "1 unresolved ambiguity" in str(info.value)
    assert "Which zone?" in str(info.value) and "--yes" in str(info.value)

    with pytest.raises(UnresolvedAmbiguityError, match="2 unresolved ambiguities"):
        resolve(ambs, fe)
    assert resolve([], fe) == []


def test_settle_writes_decisions_and_reloads(tmp_path: Path) -> None:
    path = tmp_path / "x.loom"
    path.write_text(SRC)
    fe = load(path)
    llm = FakeLLM(canned)
    fe2, answers = settle(path, fe, llm, fe.program.does, yes=True)
    assert len(answers) == 2
    assert "    # decided: weekdays are Monday to Friday" in path.read_text()
    assert [d.text for d in fe2.program.does[0].all_decided()] == [
        "all times are IST",
        "weekdays are Monday to Friday",
    ]
    none = FakeLLM(lambda r: {"ambiguities": []})
    fe3, answers = settle(path, fe2, none, fe2.program.does)
    assert answers == [] and fe3 is fe2


# --- CLI, with the synthetic model outputs ------------------------------------------------


def copy_example(tmp_path: Path, name: str) -> Path:
    dst = tmp_path / f"{name}.loom"
    shutil.copy(ROOT / "examples" / f"{name}.loom", dst)
    return dst


def test_check_yes_writes_decided_lines_then_is_stable(tmp_path: Path, synthetic_cli: None) -> None:
    path = copy_example(tmp_path, "invoice_chaser")
    res = runner.invoke(app, ["check", str(path), "--yes"])
    assert res.exit_code == 0, res.output
    text = path.read_text()
    assert "    # decided: the job runs Monday to Friday at 10:00 Asia/Kolkata time" in text
    assert text.count("# decided:") == 4
    assert "decided (chase_overdue_invoices): reminders 1 and 2 are polite" in res.output

    again = runner.invoke(app, ["check", str(path), "--yes"])
    assert again.exit_code == 0, again.output
    assert "decided" not in again.output
    assert path.read_text() == text


def test_check_without_answers_is_a_compile_error(tmp_path: Path, synthetic_cli: None) -> None:
    path = copy_example(tmp_path, "todo")
    before = path.read_text()
    res = runner.invoke(app, ["check", str(path)])
    assert res.exit_code == 1
    assert "3 unresolved ambiguities" in res.output
    assert "raise LookupError" in res.output
    assert path.read_text() == before


def test_check_interactive_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, synthetic_cli: None
) -> None:
    monkeypatch.setenv("LOOM_INTERACTIVE", "1")
    path = copy_example(tmp_path, "todo")
    # 1st question: pick option 2; 2nd: accept the default; 3rd: type a custom answer.
    res = runner.invoke(
        app, ["check", str(path)], input="2\n\ntasks without a due date are never late\n"
    )
    assert res.exit_code == 0, res.output
    assert "why it matters" in res.output and "(suggested)" in res.output
    text = path.read_text()
    assert "# decided: if no task matches, return None and change nothing" in text
    assert "# decided: the matching task is updated in place" in text
    assert "# decided: tasks without a due date are never late" in text
