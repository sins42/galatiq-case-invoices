"""Payment. Plain code, called only after a final approve decision."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from .ledger import content_hash
from .models import InvoiceData
from .normalize import number_key

Gateway = Callable[[str, float], dict[str, Any]]


def mock_payment(vendor, amount):
    print(f"Paid {amount} to {vendor}")
    return {"status": "success"}


def idempotency_key(invoice: InvoiceData) -> str:
    return f"{number_key(invoice.invoice_number)}:{content_hash(invoice)}"


def amount_paid(conn: sqlite3.Connection, invoice: InvoiceData) -> float:
    """Total already paid for this invoice number, so a revision can pay only the difference."""
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS paid FROM payments WHERE status = 'success' AND idempotency_key LIKE ?",
        (number_key(invoice.invoice_number) + ":%",),
    ).fetchone()
    return row["paid"] or 0.0


def pay(
    conn: sqlite3.Connection, invoice: InvoiceData, gateway: Gateway = mock_payment, amount: float | None = None
) -> dict[str, Any]:
    """Pay once per idempotency key. A key that was already paid returns the earlier result.
    `amount` overrides the invoice total, used to pay only the difference on a revision."""
    key = idempotency_key(invoice)
    existing = conn.execute(
        "SELECT status FROM payments WHERE idempotency_key = ? AND status = 'success'", (key,)
    ).fetchone()
    if existing:
        return {"status": "success", "reference": key, "replayed": True}

    due = invoice.total if amount is None else amount
    result = gateway(invoice.vendor, due)
    if result.get("status") == "success":
        conn.execute(
            "INSERT INTO payments (idempotency_key, vendor, amount, status, created_at) VALUES (?, ?, ?, ?, ?)",
            (key, invoice.vendor, due, "success", datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
        conn.commit()
    return {**result, "reference": key, "replayed": False}
