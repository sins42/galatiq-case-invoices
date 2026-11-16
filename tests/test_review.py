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


def test_revision_pays_only_the_difference(run, conn):
    assert run("invoice_1004.json").outcome is Outcome.PAID              # original, 1,890 paid automatically
    assert run("invoice_1004_revised.json").outcome is Outcome.ESCALATED  # revision escalates for review

    result = cli.apply_decision(conn, "INV-1004", "approve", "confirmed amendment")
    assert result["ok"] and result["paid"] == 4050.0                    # 5,940 new total minus 1,890 already paid
    amounts = sorted(r["amount"] for r in conn.execute("SELECT amount FROM payments"))
    assert amounts == [1890.0, 4050.0]
    assert _status(conn, "INV1004") == "paid"


def test_revision_at_or_below_paid_pays_nothing(run, conn, write_invoice):
    assert run("invoice_1004.json").outcome is Outcome.PAID              # 1,890 paid
    lower = write_invoice(
        {
            "invoice_number": "INV-1004",
            "revision": "R2",
            "vendor": {"name": "Precision Parts Ltd."},
            "date": "2026-01-22",
            "due_date": "2026-02-22",
            "line_items": [{"item": "WidgetA", "quantity": 1, "unit_price": 250.00}],
            "subtotal": 250.00,
            "tax_rate": 0.08,
            "tax_amount": 20.00,
            "total": 270.00,
            "currency": "USD",
        },
        name="invoice_1004_lower.json",
    )
    assert run(lower).outcome is Outcome.ESCALATED

    result = cli.apply_decision(conn, "INV-1004", "approve", "amended down")
    assert result["ok"] and result["paid"] == 0.0                       # already paid more than the new total
    assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 1  # only the original payment


def test_number_collision_is_paid_in_full_not_netted(run, conn, write_invoice):
    first = write_invoice(
        {
            "invoice_number": "INV-7777",
            "vendor": {"name": "Widgets Inc."},
            "date": "2026-01-20", "due_date": "2026-02-20", "currency": "USD",
            "line_items": [{"item": "WidgetA", "quantity": 4, "unit_price": 250.00}],
            "subtotal": 1000.00, "total": 1000.00,
        },
        name="collision_a.json",
    )
    assert run(first).outcome is Outcome.PAID                            # vendor A pays 1,000 automatically

    second = write_invoice(
        {
            "invoice_number": "INV-7777",
            "vendor": {"name": "Precision Parts Ltd."},
            "date": "2026-01-20", "due_date": "2026-02-20", "currency": "USD",
            "line_items": [{"item": "WidgetB", "quantity": 3, "unit_price": 500.00}],
            "subtotal": 1500.00, "total": 1500.00,
        },
        name="collision_b.json",
    )
    assert run(second).outcome is Outcome.ESCALATED                      # same number, different vendor

    result = cli.apply_decision(conn, "INV-7777", "approve", "separate legitimate vendor")
    assert result["paid"] == 1500.0                                     # paid in full, not netted against vendor A
    assert sorted(r["amount"] for r in conn.execute("SELECT amount FROM payments")) == [1000.0, 1500.0]
