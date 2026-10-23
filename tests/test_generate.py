from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fakes import FakeLLM

from loom.context import project_context
from loom.errors import GenerationError
from loom.frontend import load
from loom.generate import generate_block, glue_for, repair_block
from loom.llm import LLMRequest
from loom.scaffold import scaffold
from loom.tests_gen import example_tests, rule_tests

ROOT = Path(__file__).resolve().parent.parent
FE = load(ROOT / "examples" / "invoice_chaser.loom")
PROJ = scaffold(FE)
DOES = FE.program.does[0]
CTX = project_context(FE, PROJ.package, "class Invoice: ...", PROJ.has_db)

MODULE = """from datetime import datetime
from sqlmodel import Session
from invoice_chaser.runtime import guard, utcnow


def chase_overdue_invoices(session: Session, *, now: datetime | None = None) -> list[int]:
    now = now or utcnow()
    guard(True, "chase_overdue_invoices:R1")
    guard(True, "chase_overdue_invoices:R2")
    return []
"""
SCHEDULE = {"cron": "0 10 * * mon-fri", "timezone": "Asia/Kolkata"}


def scripted(*outputs: dict[str, Any]) -> FakeLLM:
    queue = list(outputs)
    return FakeLLM(lambda r: queue.pop(0))


def test_codegen_request_and_valid_module() -> None:
    llm = scripted({"module_source": MODULE, "schedule": SCHEDULE, "notes": "n"})
    mod = generate_block(llm, FE, DOES, PROJ, {"other": "def other() -> None"}, context=CTX)
    (req,) = llm.calls
    assert req.kind == "codegen" and req.effort == "medium"
    assert 'R1 -> guard id "chase_overdue_invoices:R1"' in req.prompt
    assert "`from invoice_chaser.other import other`" in req.prompt
    assert "# Generated project" in req.context
    assert mod.path == "src/invoice_chaser/chase_overdue_invoices.py"
    assert mod.schedule is not None and mod.schedule.cron == "0 10 * * mon-fri"
    glue = glue_for(mod)
    assert glue.takes_session and glue.takes_now and glue.callable_without_args


def test_invalid_module_gets_one_corrective_retry() -> None:
    bad = MODULE.replace('    guard(True, "chase_overdue_invoices:R2")\n', "")
    llm = scripted(
        {"module_source": bad, "schedule": SCHEDULE, "notes": ""},
        {"module_source": MODULE, "schedule": SCHEDULE, "notes": ""},
    )
    generate_block(llm, FE, DOES, PROJ, {}, context=CTX)
    assert len(llm.calls) == 2
    assert "rule chase_overdue_invoices:R2 has no runtime guard" in llm.calls[1].prompt


def test_invalid_twice_raises() -> None:
    bad = {"module_source": "import os\n" + MODULE, "schedule": SCHEDULE, "notes": ""}
    with pytest.raises(GenerationError, match="import of 'os' is not allowed"):
        generate_block(scripted(bad, bad), FE, DOES, PROJ, {}, context=CTX)


def test_trigger_requires_a_schedule() -> None:
    out = {"module_source": MODULE, "schedule": {"cron": "", "timezone": ""}, "notes": ""}
    with pytest.raises(GenerationError, match="no schedule"):
        generate_block(scripted(out), FE, DOES, PROJ, {}, context=CTX)


def test_repair_keeps_the_signature() -> None:
    current = generate_block(
        scripted({"module_source": MODULE, "schedule": SCHEDULE, "notes": ""}),
        FE,
        DOES,
        PROJ,
        {},
        context=CTX,
    )
    fixed = MODULE.replace("return []", "return [1]")
    llm = scripted({"module_source": fixed, "notes": "fixed"})
    new = repair_block(llm, FE, DOES, PROJ, current, "E1 failed", {}, context=CTX, attempt=1)
    assert new.source == fixed and new.schedule == current.schedule
    assert "E1 failed" in llm.calls[0].prompt and llm.calls[0].kind == "repair"
    assert "test_" not in llm.calls[0].prompt.split("Failing tests")[0]  # tests are not sent

    renamed = MODULE.replace("-> list[int]", "-> list[str]")
    with pytest.raises(GenerationError, match="signature changed"):
        repair_block(
            scripted({"module_source": renamed, "notes": ""}),
            FE,
            DOES,
            PROJ,
            current,
            "x",
            {},
            context=CTX,
            attempt=2,
        )


EX_SOURCE = """from invoice_chaser.chase_overdue_invoices import chase_overdue_invoices

def test_e1(session, now):
    chase_overdue_invoices(session, now=now)
    assert 1 == 1
"""


def test_test_translation_retries_and_validates() -> None:
    ex_mapping = [
        {
            "example": f"E{i}",
            "function": "test_e1",
            "clauses": [{"clause": f"E{i}.{c}", "check": "assert 1 == 1"} for c in range(1, n + 1)],
        }
        for i, n in ((1, 3), (2, 1), (3, 2))
    ]
    llm = scripted(
        {"module_source": EX_SOURCE, "tests": ex_mapping[:2]},
        {"module_source": EX_SOURCE, "tests": ex_mapping},
    )
    sig = "def chase_overdue_invoices(session: Session, *, now: datetime | None=None) -> list[int]"
    src = example_tests(llm, FE, DOES, PROJ, sig, context=CTX)
    assert src == EX_SOURCE
    first: LLMRequest = llm.calls[0]
    assert "E1.2: expect 1 email queued" in first.prompt and sig in first.prompt
    assert "example E3 has no test" in llm.calls[1].prompt
    assert "def chase_overdue_invoices(session" not in first.context  # no body anywhere

    rule_out = {"module_source": EX_SOURCE, "tests": [{"rule": "R1", "function": "test_e1"}]}
    with pytest.raises(GenerationError, match="rule R2 has no test"):
        rule_tests(scripted(rule_out, rule_out), FE, DOES, PROJ, sig, context=CTX)


def test_blocks_without_examples_or_rules_get_no_test_module() -> None:
    from loom.frontend import load_text

    fe = load_text("app X\ntarget: python 3.12\ndoes x\n  do it\n")
    proj = scaffold(fe)
    llm = FakeLLM(lambda r: {})
    assert example_tests(llm, fe, fe.program.does[0], proj, "def x()", context="") is None
    assert rule_tests(llm, fe, fe.program.does[0], proj, "def x()", context="") is None
    assert llm.calls == []
