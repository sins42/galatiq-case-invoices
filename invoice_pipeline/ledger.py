"""The processed-invoice ledger: content hashes, lookups, and recording outcomes."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from .models import Finding, InvoiceData, Outcome
from .normalize import item_key, number_key, vendor_key


def _line_signature(invoice: InvoiceData) -> list:
    return sorted(
        (item_key(li.name), li.quantity or 0, round(li.unit_price or 0, 2)) for li in invoice.line_items
    )


def _digest(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def bill_hash(invoice: InvoiceData) -> str:
    """Hash of what is being billed, ignoring the invoice number. Used to catch rebills."""
    return _digest(
        {
            "vendor": vendor_key(invoice.vendor),
            "currency": (invoice.currency or "").upper(),
            "total": round(invoice.total, 2) if invoice.total is not None else None,
            "lines": _line_signature(invoice),
        }
    )


def content_hash(invoice: InvoiceData) -> str:
    """Hash of the normalized invoice, so the same invoice in two file formats matches."""
    return _digest({"number": number_key(invoice.invoice_number), "bill": bill_hash(invoice)})


def find_by_number(conn: sqlite3.Connection, number: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM invoices_processed WHERE number_key = ? ORDER BY id", (number,)
    ).fetchall()


def find_by_bill(conn: sqlite3.Connection, bill: str, exclude_number: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM invoices_processed WHERE bill_hash = ? AND number_key != ? ORDER BY id",
        (bill, exclude_number),
    ).fetchall()


def find_by_vendor_total(conn: sqlite3.Connection, vendor: str, total: float) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM invoices_processed WHERE vendor_key = ? AND total = ? ORDER BY id",
        (vendor, total),
    ).fetchall()


def vendor_history(conn: sqlite3.Connection, vendor: str, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT invoice_number, total, status, created_at FROM invoices_processed "
        "WHERE vendor_key = ? ORDER BY id DESC LIMIT ?",
        (vendor_key(vendor), limit),
    ).fetchall()


def recent_vendor_total(
    conn: sqlite3.Connection, vendor: str | None, days: int, exclude_number: str | None = None
) -> float:
    """Sum of a vendor's non-rejected invoice totals processed within the last `days`, skipping
    any re-submission or revision of `exclude_number` so the same invoice is not counted twice.
    The cutoff is built in Python so it matches the stored timestamp format exactly."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    sql = (
        "SELECT COALESCE(SUM(total), 0) AS total FROM invoices_processed "
        "WHERE vendor_key = ? AND status != ? AND created_at >= ?"
    )
    params: list = [vendor_key(vendor), Outcome.REJECTED.value, cutoff]
    if exclude_number:
        sql += " AND (number_key IS NULL OR number_key != ?)"
        params.append(exclude_number)
    return conn.execute(sql, params).fetchone()["total"] or 0.0


def escalated(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM invoices_processed WHERE status = ? ORDER BY id", (Outcome.ESCALATED.value,)
    ).fetchall()


def escalated_for_number(conn: sqlite3.Connection, number: str) -> list[sqlite3.Row]:
    """Escalated rows matching an invoice number, so a reviewer can resolve them."""
    return [r for r in find_by_number(conn, number_key(number)) if r["status"] == Outcome.ESCALATED.value]


def resolve(conn: sqlite3.Connection, row_id: int, status: str, reason: str, payment_ref: str | None) -> None:
    """Record a reviewer's decision on an escalated invoice."""
    row = conn.execute("SELECT reasons FROM invoices_processed WHERE id = ?", (row_id,)).fetchone()
    detail = json.loads(row["reasons"]) if row and row["reasons"] else {"reasons": [], "findings": []}
    detail["reasons"] = (detail.get("reasons") or []) + [f"Reviewer: {reason}"]
    conn.execute(
        "UPDATE invoices_processed SET status = ?, reasons = ?, payment_ref = ? WHERE id = ?",
        (status, json.dumps(detail), payment_ref, row_id),
    )
    conn.commit()


def assign(conn: sqlite3.Connection, row_id: int, assignee: str, reason: str) -> None:
    """Route an escalated invoice to someone without resolving it. The outcome stays escalated."""
    row = conn.execute("SELECT reasons FROM invoices_processed WHERE id = ?", (row_id,)).fetchone()
    detail = json.loads(row["reasons"]) if row and row["reasons"] else {"reasons": [], "findings": []}
    detail["reasons"] = (detail.get("reasons") or []) + [f"Routed to {assignee}: {reason}"]
    conn.execute(
        "UPDATE invoices_processed SET assigned_to = ?, reasons = ? WHERE id = ?",
        (assignee, json.dumps(detail), row_id),
    )
    conn.commit()


def record(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    source_file: str,
    invoice: InvoiceData | None,
    outcome: Outcome,
    findings: list[Finding],
    reasons: list[str],
    payment_ref: str | None,
    file_format: str | None = None,
    method: str | None = None,
    source_text: str | None = None,
    approval: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO invoices_processed (run_id, source_file, invoice_number, number_key, vendor, "
        "vendor_key, content_hash, bill_hash, total, currency, status, reasons, payment_ref, "
        "invoice_json, file_format, method, source_text, approval, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            source_file,
            invoice.invoice_number if invoice else None,
            number_key(invoice.invoice_number) if invoice else None,
            invoice.vendor if invoice else None,
            vendor_key(invoice.vendor) if invoice else None,
            content_hash(invoice) if invoice else None,
            bill_hash(invoice) if invoice else None,
            invoice.total if invoice else None,
            invoice.currency if invoice else None,
            outcome.value,
            json.dumps({"reasons": reasons, "findings": [f.model_dump(mode="json") for f in findings]}),
            payment_ref,
            invoice.model_dump_json() if invoice else None,
            file_format,
            method,
            source_text,
            approval,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ),
    )
    conn.commit()
