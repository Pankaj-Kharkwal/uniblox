"""invoice_chaser.loom end to end: sqlite, outbox, scheduler, API, and the 24h rule."""

from __future__ import annotations

import shutil
from pathlib import Path

from fakes import recorded_outputs, replaying_llm
from typer.testing import CliRunner

from loom.build import BuildOptions, build
from loom.lock import Lock
from loom.loop import hash_tests

ROOT = Path(__file__).resolve().parent.parent
LABEL = "does:chase_overdue_invoices"
runner = CliRunner()


def copy_example(tmp_path: Path) -> Path:
    dst = tmp_path / "invoice_chaser.loom"
    shutil.copy(ROOT / "examples" / "invoice_chaser.loom", dst)
    return dst


def test_invoice_chaser_builds_green_with_guard_test_scheduler_and_api(tmp_path: Path) -> None:
    from loom.cli import app

    src = copy_example(tmp_path)
    out = tmp_path / "out" / "invoice_chaser"
    res = runner.invoke(app, ["build", str(src), "--yes", "--out", str(out)])
    assert res.exit_code == 0, res.output
    assert "GREEN: 5 passed" in res.output
    assert src.read_text().count("# decided:") == 4

    pkg = out / "src" / "invoice_chaser"
    module = (pkg / "chase_overdue_invoices.py").read_text()
    # The 24h rule: a runtime guard in the code...
    assert '"chase_overdue_invoices:R1"' in module and "guard(" in module
    # ...and a property test.
    rules = (out / "tests" / "test_chase_overdue_invoices_rules.py").read_text()
    assert "def test_r1_never_email_the_same_client_twice_in_24h" in rules
    assert "@given(" in rules
    # Emails are queued in an outbox table, not sent.
    assert (
        "queue_email(" in module
        and "class OutboxEmail(SQLModel, table=True)" in (pkg / "outbox.py").read_text()
    )
    # Scheduler stub with the decided schedule, and an API entry point.
    scheduler = (pkg / "scheduler.py").read_text()
    assert "'0 10 * * mon-fri', 'Asia/Kolkata'" in scheduler
    assert '@app.post("/jobs/chase_overdue_invoices")' in (pkg / "api.py").read_text()

    lock = Lock.load(tmp_path / "invoice_chaser.loom.lock")
    assert lock is not None
    entry = lock.blocks[LABEL]
    assert entry.schedule == {"cron": "0 10 * * mon-fri", "timezone": "Asia/Kolkata"}
    assert entry.signature == (
        "def chase_overdue_invoices(session: Session, *, now: datetime | None=None) -> ChaseResult"
    )

    again = runner.invoke(app, ["build", str(src), "--yes"])
    assert again.exit_code == 0 and "nothing to rebuild" in again.output


def test_broken_example_goes_red_after_max_iters_and_its_test_is_unchanged(
    tmp_path: Path,
) -> None:
    src = copy_example(tmp_path)
    # Contradict the "always skip paid invoices" rule in example 3.
    src.write_text(
        src.read_text().replace(
            "expect status paid, 0 emails queued", "expect status paid, 1 email queued"
        )
    )
    recorded = recorded_outputs()
    ex = recorded[("example_tests", LABEL)]
    head, e3 = ex["module_source"].split("def test_e3_paid_invoice_is_left_alone")
    broken_tests = {
        "module_source": head
        + "def test_e3_paid_invoice_is_left_alone"
        + e3.replace(
            "assert len(queued_emails(session)) == 0", "assert len(queued_emails(session)) == 1"
        ),
        "tests": [
            *ex["tests"][:2],
            {
                "example": "E3",
                "function": "test_e3_paid_invoice_is_left_alone",
                "clauses": [
                    {"clause": "E3.1", "check": "assert invoice.status == InvoiceStatus.PAID"},
                    {"clause": "E3.2", "check": "assert len(queued_emails(session)) == 1"},
                ],
            },
        ],
    }
    same_code = {
        "module_source": recorded[("codegen", LABEL)]["module_source"],
        "notes": "The example contradicts rule R2 (always skip paid invoices); the rule wins.",
    }
    llm = replaying_llm({("example_tests", LABEL): broken_tests, ("repair", LABEL): same_code})
    out = tmp_path / "out"
    report = build(
        src, lambda: llm, BuildOptions(out_dir=out, yes=True, max_iters=3, echo=lambda s: None)
    )

    assert not report.ok
    loop = report.loop
    assert loop is not None and loop.rounds == 3
    assert loop.iterations == {"chase_overdue_invoices": 3}
    failing = {c.name for c in loop.result.failures}
    assert failing == {"test_e3_paid_invoice_is_left_alone"}
    examples_file = "tests/test_chase_overdue_invoices_examples.py"
    assert hash_tests(out, [examples_file])[examples_file] == loop.test_hashes[examples_file]
    assert "assert len(queued_emails(session)) == 1" in (out / examples_file).read_text()
    lock = Lock.load(tmp_path / "invoice_chaser.loom.lock")
    assert lock is not None and lock.blocks[LABEL].status == "red"
