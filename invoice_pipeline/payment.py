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


def pay(conn: sqlite3.Connection, invoice: InvoiceData, gateway: Gateway = mock_payment) -> dict[str, Any]:
    """Pay once per idempotency key. A key that was already paid returns the earlier result."""
    key = idempotency_key(invoice)
    existing = conn.execute(
        "SELECT status FROM payments WHERE idempotency_key = ? AND status = 'success'", (key,)
    ).fetchone()
    if existing:
        return {"status": "success", "reference": key, "replayed": True}

    result = gateway(invoice.vendor, invoice.total)
    if result.get("status") == "success":
        conn.execute(
            "INSERT INTO payments (idempotency_key, vendor, amount, status, created_at) VALUES (?, ?, ?, ?, ?)",
            (key, invoice.vendor, invoice.total, "success", datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
        conn.commit()
    return {**result, "reference": key, "replayed": False}
