"""The human review flow: approving and rejecting escalated invoices."""

from invoice_pipeline import cli, ledger
from invoice_pipeline.models import Outcome


def _status(conn, number_key: str) -> str:
    return conn.execute(
        "SELECT status FROM invoices_processed WHERE number_key = ?", (number_key,)
    ).fetchone()["status"]


def test_escalated_invoice_can_be_approved_and_paid(run, conn):
    assert run("invoice_1002.txt").outcome is Outcome.ESCALATED
    cli.resolve_invoice(conn, "1002", "approve", "VP signed off")
    assert ledger.escalated_for_number(conn, "1002") == []
    assert _status(conn, "1002") == "paid"
    assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 1


def test_escalated_invoice_can_be_rejected(run, conn):
    assert run("invoice_1002.txt").outcome is Outcome.ESCALATED
    cli.resolve_invoice(conn, "1002", "reject", "not a known vendor")
    assert _status(conn, "1002") == "rejected"
    assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 0


def test_resolving_an_unknown_invoice_is_safe(conn):
    cli.resolve_invoice(conn, "INV-9999", "approve", "x")  # must not raise
    assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 0
