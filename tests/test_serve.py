"""The live workbench server: the decision endpoint and the ledger-backed view model."""

import json

from conftest import SAMPLES

from invoice_pipeline import serve, workbench
from invoice_pipeline.graph import InvoicePipeline
from invoice_pipeline.llm import MockProvider


def _escalate(run, name="invoice_1005.json"):
    return run(name).invoice.invoice_number  # INV-1005, escalated on OVERSTOCK


def _status(conn):
    return conn.execute("SELECT status, assigned_to FROM invoices_processed WHERE number_key = ?", ("INV1005",)).fetchone()


def test_endpoint_approve_pays_once(run, conn):
    number = _escalate(run)
    code, result = serve.handle_decision(conn, {"invoice_number": number, "decision": "approve", "reason": "ok"})

    assert code == 200 and result["ok"] and result["status"] == "paid"
    assert conn.execute("SELECT COUNT(*) AS c FROM payments").fetchone()["c"] == 1
    assert _status(conn)["status"] == "paid"
    assert result["row"]["status"] == "paid" and result["row"]["payment_ref"]  # updated row is returned


def test_endpoint_reject_writes_reason(run, conn):
    number = _escalate(run)
    code, result = serve.handle_decision(conn, {"invoice_number": number, "decision": "reject", "reason": "not approved"})

    assert code == 200 and result["status"] == "rejected"
    row = conn.execute("SELECT reasons FROM invoices_processed WHERE number_key = ?", ("INV1005",)).fetchone()
    assert any("not approved" in r for r in json.loads(row["reasons"])["reasons"])


def test_endpoint_route_assigns_without_paying(run, conn):
    number = _escalate(run)
    code, result = serve.handle_decision(conn, {"invoice_number": number, "decision": "route", "reason": "controller please"})

    assert code == 200 and result["status"] == "routed"
    row = _status(conn)
    assert row["assigned_to"] == "controller" and row["status"] == "escalated"
    assert conn.execute("SELECT COUNT(*) AS c FROM payments").fetchone()["c"] == 0
    assert result["row"]["assigned_to"] == "controller"  # updated row is returned


def test_endpoint_rejects_missing_reason_and_bad_input(run, conn):
    number = _escalate(run)
    assert serve.handle_decision(conn, {"invoice_number": number, "decision": "reject", "reason": ""})[0] == 400
    assert serve.handle_decision(conn, {"invoice_number": number, "decision": "route", "reason": "  "})[0] == 400
    assert serve.handle_decision(conn, {"invoice_number": number, "decision": "sideways", "reason": "x"})[0] == 400
    assert serve.handle_decision(conn, {"decision": "approve", "reason": "x"})[0] == 400  # no invoice_number
    assert _status(conn)["status"] == "escalated"  # nothing was changed


def test_ledger_view_model_tracks_routing(settings, conn):
    pipeline = InvoicePipeline(settings, MockProvider(), conn)
    for path in sorted(p for p in SAMPLES.iterdir() if p.is_file()):
        pipeline.run(path, run_id="live")

    vm = workbench.build_view_model_from_ledger(conn, settings)
    assert vm["summary"]["escalated"]["count"] == 10
    assert len(vm["queue"]) == 10
    assert all(not q["routed"] for q in vm["queue"])

    serve.handle_decision(conn, {"invoice_number": "INV-1005", "decision": "route", "reason": "controller please"})
    after = workbench.build_view_model_from_ledger(conn, settings)
    routed = [q for q in after["queue"] if q["routed"]]
    assert len(routed) == 1 and routed[0]["owner"] == "Controller"
    assert len(after["queue"]) == 10  # routing keeps it in the queue
