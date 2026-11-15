"""Command line interface."""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

from . import db, ledger, logs
from .config import load_settings
from .graph import InvoicePipeline
from .llm import LLMError, make_provider
from .state import PipelineState


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Process supplier invoices: ingest, validate, approve, pay.")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--invoice_path", metavar="PATH", help="invoice file to process")
    action.add_argument("--review", action="store_true", help="list invoices waiting for human review")
    action.add_argument("--reset", action="store_true", help="clear the ledger and reseed the database")
    parser.add_argument("--offline", action="store_true", help="use the offline provider even if an API key is set")
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
    settings = load_settings(**({"db_path": Path(args.db)} if args.db else {}))
    conn = db.connect(settings.db_path)
    db.init_db(conn, reset=args.reset)

    if args.reset:
        print(f"Database reset: {settings.db_path}")
        return 0
    if args.review:
        print_review_queue(conn)
        return 0

    try:
        provider = make_provider(settings, offline=args.offline)
    except LLMError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    pipeline = InvoicePipeline(settings, provider, conn)
    state = pipeline.run(args.invoice_path, run_id=uuid.uuid4().hex[:8])
    print_result(state)
    return 0
