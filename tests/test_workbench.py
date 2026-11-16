"""The review workbench: the view model built from a batch, and the shared decision path."""

import dataclasses
import datetime as dt
import json
from collections import Counter

from conftest import SAMPLES

from invoice_pipeline import cli, metrics, workbench
from invoice_pipeline.graph import InvoicePipeline
from invoice_pipeline.llm import MockProvider
from invoice_pipeline.validation import load_catalog


def _run_full_batch(settings, conn):
    provider = MockProvider()
    pipeline = InvoicePipeline(settings, provider, conn)
    files = sorted(p for p in SAMPLES.iterdir() if p.is_file())
    states = [pipeline.run(path, run_id="test") for path in files]
    return states, provider


def test_view_model_for_the_sample_batch(settings, conn):
    settings = dataclasses.replace(settings, as_of_date=dt.date(2026, 1, 29))
    states, provider = _run_full_batch(settings, conn)
    run_metrics = metrics.summarize(states, provider.usage, settings)

    vm = workbench.build_view_model(states, run_metrics, settings, load_catalog(conn))

    assert vm["summary"]["paid"]["count"] == 6
    assert vm["summary"]["rejected"]["count"] == 4
    assert vm["summary"]["escalated"]["count"] == 10
    assert len(vm["queue"]) == 10

    groups = Counter(q["due_group"] for q in vm["queue"])
    assert groups == {"overdue": 2, "soon": 1, "later": 7}

    owners = Counter(q["owner"] for q in vm["queue"])
    assert owners == {"Controller": 5, "AP clerk": 5}

    # Every escalated invoice carries a reason and its extracted lines.
    assert all(q["why"] and q["lines"] for q in vm["queue"])
    # The urgent list is the overdue and due-soon invoices, soonest first.
    assert len(vm["summary"]["urgent"]) == 3


def test_asof_flag_parses_and_rejects_bad_dates(tmp_path):
    db = str(tmp_path / "a.db")
    assert cli.main(["--review", "--as-of", "2026-01-29", "--db", db]) == 0
    assert cli.main(["--review", "--as-of", "not-a-date", "--db", db]) == 2


def test_render_is_complete_at_rest(settings, conn):
    states, provider = _run_full_batch(settings, conn)
    html = workbench.render(workbench.build_view_model(states, metrics.summarize(states, [], settings), settings, load_catalog(conn)))

    assert html.startswith("<!doctype html>")
    assert "Invoice Review Workbench" in html
    assert "Needs your review" in html
    assert "OVERSTOCK" in html  # a finding code is shown as a flag chip
    assert "Atlas Industrial Supply" in html  # a queued vendor is rendered, not blank


def _escalate_one(run, name="invoice_1005.json"):
    state = run(name)
    return state.invoice.invoice_number


def test_approve_pays_exactly_once(run, conn):
    number = _escalate_one(run)
    result = cli.apply_decision(conn, number, "approve", "confirmed with vendor")

    assert result["ok"] and result["status"] == "paid"
    assert conn.execute("SELECT COUNT(*) AS c FROM payments").fetchone()["c"] == 1
    assert conn.execute(
        "SELECT status FROM invoices_processed WHERE number_key = ?", ("INV1005",)
    ).fetchone()["status"] == "paid"


def test_reject_writes_the_reason(run, conn):
    number = _escalate_one(run)
    result = cli.apply_decision(conn, number, "reject", "vendor not approved")

    assert result["ok"] and result["status"] == "rejected"
    row = conn.execute("SELECT status, reasons FROM invoices_processed WHERE number_key = ?", ("INV1005",)).fetchone()
    assert row["status"] == "rejected"
    assert any("vendor not approved" in r for r in json.loads(row["reasons"])["reasons"])


def test_route_assigns_without_paying(run, conn):
    number = _escalate_one(run)
    result = cli.apply_decision(conn, number, "route", "needs controller sign-off")

    assert result["ok"] and result["status"] == "routed"
    row = conn.execute(
        "SELECT status, assigned_to FROM invoices_processed WHERE number_key = ?", ("INV1005",)
    ).fetchone()
    assert row["assigned_to"] == "controller"
    assert row["status"] == "escalated"  # routing does not resolve the invoice
    assert conn.execute("SELECT COUNT(*) AS c FROM payments").fetchone()["c"] == 0


def test_reject_and_route_require_a_reason(run, conn):
    number = _escalate_one(run)
    assert cli.apply_decision(conn, number, "reject", "   ")["ok"] is False
    assert cli.apply_decision(conn, number, "route", "")["ok"] is False
    # The invoice is untouched, still waiting for review.
    assert conn.execute(
        "SELECT status FROM invoices_processed WHERE number_key = ?", ("INV1005",)
    ).fetchone()["status"] == "escalated"
