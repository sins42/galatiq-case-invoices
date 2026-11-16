"""Command line interface."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import uuid
from pathlib import Path

from . import db, ledger, logs, metrics, payment, readers, workbench
from .config import load_settings
from .graph import InvoicePipeline
from .llm import LLMError, Provider, make_provider
from .models import InvoiceData, Outcome
from .state import PipelineState


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Process supplier invoices: ingest, validate, approve, pay.")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--invoice_path", metavar="PATH", help="invoice file to process")
    action.add_argument("--batch", metavar="DIR", help="process every supported invoice in a directory")
    action.add_argument("--review", action="store_true", help="list invoices waiting for human review")
    action.add_argument("--approve", metavar="NUMBER", help="approve and pay an escalated invoice")
    action.add_argument("--reject", metavar="NUMBER", help="reject an escalated invoice")
    action.add_argument("--route", metavar="NUMBER", help="route an escalated invoice to the Controller")
    action.add_argument("--serve", action="store_true", help="serve the live review workbench on localhost")
    action.add_argument("--reset", action="store_true", help="clear the ledger and reseed the database")
    parser.add_argument("--reason", default="", help="reason recorded with --approve, --reject, or --route")
    parser.add_argument("--port", type=int, default=8765, help="port for --serve (default 8765)")
    parser.add_argument("--as-of", metavar="YYYY-MM-DD", help="reference date for due-date grouping and date checks (default: today)")
    parser.add_argument("--report", metavar="PATH", help="write an HTML run report to PATH")
    parser.add_argument("--offline", action="store_true", help="use the offline provider even if an API key is set")
    parser.add_argument("--record", metavar="PATH", help="record the live Grok responses to PATH for later replay")
    parser.add_argument("--replay", metavar="PATH", help="replay recorded model responses from PATH (no API key needed)")
    parser.add_argument("--db", metavar="PATH", help="SQLite database path (default: inventory.db)")
    parser.add_argument("--log-level", default="INFO", help="log level for the structured logs on stderr")
    return parser


def print_result(state: PipelineState) -> None:
    invoice = state.invoice
    print()
    if invoice:
        total = f"{invoice.total:,.2f} {invoice.currency or ''}".strip() if invoice.total is not None else "no total"
        print(f"Invoice   {invoice.invoice_number or '(no number)'}  |  {invoice.vendor or '(no vendor)'}  |  {total}")
    print(f"Outcome   {state.outcome.value.upper()}")
    if state.findings:
        print("Findings")
        for finding in state.findings:
            print(f"  [{finding.severity.value}] {finding.code}: {finding.message}")
    print("Reasons")
    for reason in state.reasons:
        print(f"  - {reason}")
    print(f"Audit     {os.path.relpath(state.audit_file)}")


def run_batch(pipeline: InvoicePipeline, directory: Path, provider: Provider, settings, conn, report_path: str | None) -> None:
    if not directory.is_dir():
        print(f"Not a directory: {directory}")
        return
    files = sorted(p for p in directory.iterdir() if p.is_file() and p.suffix.lower().lstrip(".") in readers.READERS)
    if not files:
        print(f"No supported invoices in {directory}")
        return
    states = []
    for path in files:
        state = pipeline.run(path, run_id=uuid.uuid4().hex[:8])
        states.append(state)
        total = f"{state.invoice.total:,.2f}" if state.invoice and state.invoice.total is not None else "-"
        number = state.invoice.invoice_number if state.invoice else None
        print(f"{path.name:28} {state.outcome.value.upper():10} {number or '-':14} {total}")
    print()
    run_metrics = metrics.summarize(states, provider.usage, settings)
    print(metrics.format_metrics(run_metrics))
    if report_path:
        workbench.write(Path(report_path), states, run_metrics, settings, conn)
        print(f"\nReport     {os.path.relpath(report_path)}")


def _public_row(conn, row_id: int) -> dict | None:
    """The ledger row after a decision, trimmed to the fields a caller needs (no raw JSON blobs)."""
    row = conn.execute(
        "SELECT id, invoice_number, status, total, currency, payment_ref, assigned_to "
        "FROM invoices_processed WHERE id = ?",
        (row_id,),
    ).fetchone()
    return dict(row) if row else None


def apply_decision(conn, number: str, decision: str, reason: str) -> dict:
    """Resolve or route an escalated invoice. Shared by the CLI and the live workbench endpoint,
    so both paths go through the same payment and ledger writes. On success the result carries the
    updated ledger row under 'row'."""
    rows = ledger.escalated_for_number(conn, number)
    if not rows:
        return {"ok": False, "error": f"No escalated invoice found for {number}."}
    if decision in ("reject", "route") and not reason.strip():
        return {"ok": False, "error": "A reason is required to reject or route."}
    row = rows[-1]
    if decision == "approve":
        if not row["invoice_json"]:
            return {"ok": False, "error": f"{number} has no stored invoice data to pay."}
        invoice = InvoiceData.model_validate_json(row["invoice_json"])
        total = invoice.total or 0
        detail = json.loads(row["reasons"]) if row["reasons"] else {"findings": []}
        is_revision = any(f.get("code") == "REVISION_OF_PRIOR" for f in detail.get("findings", []))
        # Net only for a genuine revision; a number shared across vendors is paid in full.
        already = payment.amount_paid(conn, invoice) if is_revision else 0.0
        due = round(total - already, 2)
        msg = reason or "Approved by reviewer"
        if already > 0 and due <= 0:
            ledger.resolve(conn, row["id"], Outcome.PAID.value, f"{msg}; no payment due, already settled", None)
            return {"ok": True, "status": "paid", "paid": 0.0, "reference": None, "row": _public_row(conn, row["id"])}
        pay_amount = due if already > 0 else total
        result = payment.pay(conn, invoice, payment.mock_payment, amount=pay_amount)
        if result.get("status") != "success":
            return {"ok": False, "error": f"{number} payment failed, nothing was paid."}
        if already > 0:
            msg = f"{msg}; paid difference {pay_amount:,.2f} (new {total:,.2f} over prior {already:,.2f})"
        ledger.resolve(conn, row["id"], Outcome.PAID.value, msg, result["reference"])
        return {"ok": True, "status": "paid", "paid": pay_amount, "reference": result["reference"], "row": _public_row(conn, row["id"])}
    if decision == "reject":
        ledger.resolve(conn, row["id"], Outcome.REJECTED.value, reason, None)
        return {"ok": True, "status": "rejected", "row": _public_row(conn, row["id"])}
    if decision == "route":
        ledger.assign(conn, row["id"], "controller", reason)
        return {"ok": True, "status": "routed", "row": _public_row(conn, row["id"])}
    return {"ok": False, "error": f"Unknown decision '{decision}'."}


def resolve_invoice(conn, number: str, decision: str, reason: str) -> None:
    result = apply_decision(conn, number, decision, reason)
    if not result["ok"]:
        print(result["error"])
        return
    verb = {"paid": "approved and paid", "rejected": "rejected", "routed": "routed to the Controller"}[result["status"]]
    print(f"{number} {verb}." + (f" Reason: {reason}" if reason.strip() else ""))


def print_review_queue(conn) -> None:
    rows = ledger.escalated(conn)
    if not rows:
        print("No invoices are waiting for review.")
        return
    print(f"{len(rows)} invoice(s) waiting for review\n")
    for row in rows:
        total = f"{row['total']:,.2f}" if row["total"] is not None else "-"
        print(f"{row['invoice_number'] or '(no number)'}  |  {row['vendor'] or '(no vendor)'}  |  {total}  |  {row['source_file']}")
        for reason in json.loads(row["reasons"])["reasons"]:
            print(f"    - {reason}")
        print()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logs.configure(args.log_level)
    overrides: dict = {}
    if args.db:
        overrides["db_path"] = Path(args.db)
    if args.as_of:
        try:
            overrides["as_of_date"] = dt.date.fromisoformat(args.as_of)
        except ValueError:
            print(f"error: --as-of must be YYYY-MM-DD, got {args.as_of!r}", file=sys.stderr)
            return 2
    settings = load_settings(**overrides)
    conn = db.connect(settings.db_path)
    db.init_db(conn, reset=args.reset)

    if args.reset:
        print(f"Database reset: {settings.db_path}")
        return 0
    if args.review:
        print_review_queue(conn)
        return 0
    if args.approve:
        resolve_invoice(conn, args.approve, "approve", args.reason)
        return 0
    if args.reject:
        resolve_invoice(conn, args.reject, "reject", args.reason)
        return 0
    if args.route:
        resolve_invoice(conn, args.route, "route", args.reason)
        return 0
    if args.serve:
        from . import serve  # imported lazily so the CLI does not pull in the server unless asked
        serve.run(settings, args.port)
        return 0

    try:
        provider = make_provider(settings, offline=args.offline, record_path=args.record, replay_path=args.replay)
    except LLMError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    pipeline = InvoicePipeline(settings, provider, conn)
    if args.batch:
        run_batch(pipeline, Path(args.batch), provider, settings, conn, args.report)
        return 0
    state = pipeline.run(args.invoice_path, run_id=uuid.uuid4().hex[:8])
    print_result(state)
    if args.report:
        workbench.write(Path(args.report), [state], metrics.summarize([state], provider.usage, settings), settings, conn)
        print(f"Report     {os.path.relpath(args.report)}")
    return 0
