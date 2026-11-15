"""End-to-end runs over the sample invoices, using the offline provider."""

import pytest

from invoice_pipeline.models import Outcome

EXPECTED = [
    ("invoice_1001.txt", Outcome.PAID, set()),
    ("invoice_1002.txt", Outcome.ESCALATED, {"OVERSTOCK"}),
    ("invoice_1003.txt", Outcome.ESCALATED, {"ZERO_STOCK"}),
    ("invoice_1004.json", Outcome.PAID, set()),
    ("invoice_1005.json", Outcome.ESCALATED, {"OVERSTOCK"}),
    ("invoice_1006.csv", Outcome.PAID, set()),
    ("invoice_1007.csv", Outcome.ESCALATED, {"OVERSTOCK"}),
    ("invoice_1008.txt", Outcome.ESCALATED, {"UNKNOWN_ITEM"}),
    ("invoice_1009.json", Outcome.REJECTED, {"MISSING_VENDOR", "INVALID_QUANTITY", "NEGATIVE_TOTAL"}),
    ("invoice_1010.txt", Outcome.ESCALATED, {"PRICE_ABOVE_CATALOG"}),
    ("invoice_1011.txt", Outcome.PAID, set()),
    ("invoice_1011.pdf", Outcome.PAID, set()),
    ("invoice_1012.txt", Outcome.PAID, set()),
    ("invoice_1012.pdf", Outcome.PAID, set()),
    ("invoice_1013.json", Outcome.ESCALATED, {"OVERSTOCK", "TOTAL_OVERCHARGE"}),
    ("invoice_1013.pdf", Outcome.ESCALATED, {"OVERSTOCK", "TOTAL_OVERCHARGE"}),
    ("invoice_1014.xml", Outcome.ESCALATED, {"FOREIGN_CURRENCY"}),
    ("invoice_1015.csv", Outcome.PAID, set()),
    ("invoice_1016.json", Outcome.ESCALATED, {"UNKNOWN_ITEM"}),
]


@pytest.mark.parametrize(("sample", "outcome", "codes"), EXPECTED)
def test_sample_outcomes(run, sample, outcome, codes):
    state = run(sample)
    assert state.outcome is outcome
    assert codes <= {f.code for f in state.findings}


def test_structured_files_skip_the_model(run):
    assert run("invoice_1004.json").method == "structured"
    assert run("invoice_1006.csv").method == "structured"
    assert run("invoice_1014.xml").method == "structured"
    assert run("invoice_1001.txt").method == "model"
    assert run("invoice_1011.pdf").method == "model"


def test_missing_currency_defaults_to_home_currency(run):
    # The CSV samples state no currency, so they are treated as USD.
    assert run("invoice_1006.csv").invoice.currency == "USD"


def test_same_invoice_in_two_formats_is_a_duplicate(run):
    assert run("invoice_1011.txt").outcome is Outcome.PAID
    state = run("invoice_1011.pdf")
    assert state.outcome is Outcome.REJECTED
    assert "DUPLICATE_INVOICE" in {f.code for f in state.findings}


def test_rush_order_qualifier_matches_catalog_item(run):
    state = run("invoice_1010.txt")
    assert "UNKNOWN_ITEM" not in {f.code for f in state.findings}
    assert state.proposal is not None, "price warning should send the invoice to the approval agent"


def test_revision_escalates_with_the_difference(run):
    run("invoice_1004.json")
    state = run("invoice_1004_revised.json")
    assert state.outcome is Outcome.ESCALATED
    revision = next(f for f in state.findings if f.code == "REVISION_OF_PRIOR")
    assert "+4,050.00" in revision.message


def test_exact_duplicate_is_rejected_and_not_paid_twice(run, conn):
    assert run("invoice_1004.json").outcome is Outcome.PAID
    state = run("invoice_1004.json")
    assert state.outcome is Outcome.REJECTED
    assert {f.code for f in state.findings} == {"DUPLICATE_INVOICE"}
    assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 1


def test_unsupported_format_escalates(run, tmp_path):
    bad = tmp_path / "invoice.docx"
    bad.write_text("not a supported format")
    state = run(bad)
    assert state.outcome is Outcome.ESCALATED
    assert "Unsupported format" in state.reasons[0]


def test_split_billing_escalates_on_repeated_vendor_invoices(run, write_invoice):
    base = {"vendor": {"name": "Widgets Inc."}, "date": "2026-01-15", "due_date": "2026-02-15", "currency": "USD"}
    first = base | {
        "invoice_number": "INV-8001",
        "line_items": [
            {"item": "WidgetA", "quantity": 4, "unit_price": 250.0},
            {"item": "WidgetB", "quantity": 10, "unit_price": 500.0},
        ],
        "total": 6000.0,
    }
    second = base | {
        "invoice_number": "INV-8002",
        "line_items": [
            {"item": "GadgetX", "quantity": 5, "unit_price": 750.0},
            {"item": "WidgetA", "quantity": 5, "unit_price": 250.0},
        ],
        "total": 5000.0,
    }
    assert run(write_invoice(first, "a.json")).outcome is Outcome.PAID
    state = run(write_invoice(second, "b.json"))
    assert "SPLIT_BILLING" in {f.code for f in state.findings}
    assert state.outcome is Outcome.ESCALATED


def test_every_run_writes_an_audit_record(run, conn):
    state = run("invoice_1002.txt")
    assert state.audit_file and state.audit[-1].stage == "finalize"
    assert conn.execute("SELECT status FROM invoices_processed").fetchone()["status"] == "escalated"
