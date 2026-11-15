"""End-to-end runs over the sample invoices, using the offline provider."""

import pytest

from invoice_pipeline.models import Outcome

EXPECTED = [
    ("invoice_1001.txt", Outcome.PAID, set()),
    ("invoice_1002.txt", Outcome.ESCALATED, {"OVERSTOCK"}),
    ("invoice_1003.txt", Outcome.ESCALATED, {"ZERO_STOCK"}),
    ("invoice_1004.json", Outcome.PAID, set()),
    ("invoice_1005.json", Outcome.ESCALATED, {"OVERSTOCK"}),
    ("invoice_1008.txt", Outcome.ESCALATED, {"UNKNOWN_ITEM"}),
    ("invoice_1009.json", Outcome.REJECTED, {"MISSING_VENDOR", "INVALID_QUANTITY", "NEGATIVE_TOTAL"}),
    ("invoice_1010.txt", Outcome.ESCALATED, {"PRICE_ABOVE_CATALOG"}),
    ("invoice_1011.txt", Outcome.PAID, set()),
    ("invoice_1012.txt", Outcome.PAID, set()),
    ("invoice_1013.json", Outcome.ESCALATED, {"OVERSTOCK", "TOTAL_OVERCHARGE"}),
    ("invoice_1016.json", Outcome.ESCALATED, {"UNKNOWN_ITEM"}),
]


@pytest.mark.parametrize(("sample", "outcome", "codes"), EXPECTED)
def test_sample_outcomes(run, sample, outcome, codes):
    state = run(sample)
    assert state.outcome is outcome
    assert codes <= {f.code for f in state.findings}


def test_structured_files_skip_the_model(run):
    assert run("invoice_1004.json").method == "structured"
    assert run("invoice_1001.txt").method == "model"


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


def test_unsupported_format_escalates(run):
    state = run("invoice_1006.csv")
    assert state.outcome is Outcome.ESCALATED
    assert "Unsupported format" in state.reasons[0]


def test_every_run_writes_an_audit_record(run, conn):
    state = run("invoice_1002.txt")
    assert state.audit_file and state.audit[-1].stage == "finalize"
    assert conn.execute("SELECT status FROM invoices_processed").fetchone()["status"] == "escalated"
