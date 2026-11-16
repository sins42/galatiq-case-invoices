"""Parsers for structured non-text formats: CSV and XML.

Each parser returns a dict in the same shape as the JSON invoices, so
`extraction.from_structured` maps all structured formats the same way. A parser
returns None when the content does not match a known layout, and the caller then
falls back to the model path.
"""

from __future__ import annotations

import csv
import io
from typing import Any

import defusedxml.ElementTree as ET

from .normalize import parse_amount, parse_rate


def _to_int(value: str | None) -> int | None:
    amount = parse_amount(value)
    return int(amount) if amount is not None else None


def _invoice(**fields: Any) -> dict[str, Any]:
    """Fill in the full set of keys so from_structured sees a uniform shape."""
    base = {
        "invoice_number": None,
        "vendor": None,
        "date": None,
        "due_date": None,
        "currency": None,
        "line_items": [],
        "subtotal": None,
        "tax": None,
        "tax_rate": None,
        "shipping": None,
        "total": None,
        "payment_terms": None,
        "notes": None,
    }
    return base | fields


# -- CSV --------------------------------------------------------------------


def parse_csv(text: str) -> dict[str, Any] | None:
    rows = [row for row in csv.reader(io.StringIO(text)) if any(cell.strip() for cell in row)]
    if not rows:
        return None
    header = [cell.strip().lower() for cell in rows[0]]
    if header[:2] == ["field", "value"]:
        return _parse_kv_csv(rows[1:])
    if "item" in header and ("qty" in header or "quantity" in header):
        return _parse_wide_csv(header, rows[1:])
    return None


def _parse_kv_csv(data_rows: list[list[str]]) -> dict[str, Any]:
    """Vertical layout: one field per row, repeated item/quantity/unit_price blocks."""
    fields: dict[str, str] = {}
    items: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for row in data_rows:
        if len(row) < 2 or not row[0].strip():
            continue
        key, value = row[0].strip().lower(), row[1].strip()
        if key == "item":
            current = {"name": value, "quantity": None, "unit_price": None, "amount": None}
            items.append(current)
        elif key in ("quantity", "qty") and current is not None:
            current["quantity"] = _to_int(value)
        elif key in ("unit_price", "unit price", "price") and current is not None:
            current["unit_price"] = parse_amount(value)
        elif key in ("amount", "line_total", "line total") and current is not None:
            current["amount"] = parse_amount(value)
        else:
            fields[key] = value
    return _invoice(
        invoice_number=fields.get("invoice_number") or fields.get("invoice number"),
        vendor=fields.get("vendor"),
        date=fields.get("date") or fields.get("invoice_date"),
        due_date=fields.get("due_date") or fields.get("due date"),
        currency=fields.get("currency"),
        line_items=items,
        subtotal=parse_amount(fields.get("subtotal")),
        tax=parse_amount(fields.get("tax")),
        tax_rate=parse_rate(fields.get("tax_rate") or fields.get("tax rate")),
        shipping=parse_amount(fields.get("shipping")),
        total=parse_amount(fields.get("total")),
        payment_terms=fields.get("payment_terms") or fields.get("payment terms"),
    )


def _parse_wide_csv(header: list[str], data_rows: list[list[str]]) -> dict[str, Any]:
    """Tabular layout: one row per line item, invoice fields repeated, totals in a footer."""
    index = {name: i for i, name in enumerate(header)}

    def cell(row: list[str], *names: str) -> str:
        for name in names:
            i = index.get(name)
            if i is not None and i < len(row) and row[i].strip():
                return row[i].strip()
        return ""

    items: list[dict[str, Any]] = []
    meta: dict[str, str] = {}
    subtotal = tax = total = tax_rate = None
    for row in data_rows:
        name = cell(row, "item")
        if name:
            items.append(
                {
                    "name": name,
                    "quantity": _to_int(cell(row, "qty", "quantity")),
                    "unit_price": parse_amount(cell(row, "unit price", "unit_price")),
                    "amount": parse_amount(cell(row, "line total", "line_total", "amount")),
                }
            )
            meta.setdefault("invoice_number", cell(row, "invoice number", "invoice_number"))
            meta.setdefault("vendor", cell(row, "vendor"))
            meta.setdefault("date", cell(row, "date"))
            meta.setdefault("due_date", cell(row, "due date", "due_date"))
            continue
        # Footer row: a label such as "Subtotal:" or "Tax (6%):" with its value in the next cell.
        for i, raw in enumerate(row):
            label = raw.strip().lower().rstrip(":")
            value = row[i + 1].strip() if i + 1 < len(row) else ""
            if label == "subtotal":
                subtotal = parse_amount(value)
            elif label.startswith("tax"):
                tax = parse_amount(value)
                tax_rate = parse_rate(raw)  # the rate, if any, is written in the label: "Tax (6%)"
            elif label == "total":
                total = parse_amount(value)
    return _invoice(
        invoice_number=meta.get("invoice_number") or None,
        vendor=meta.get("vendor") or None,
        date=meta.get("date") or None,
        due_date=meta.get("due_date") or None,
        line_items=items,
        subtotal=subtotal,
        tax=tax,
        tax_rate=tax_rate,
        total=total,
    )


# -- XML --------------------------------------------------------------------


def _text(element: Any, tag: str) -> str | None:
    found = element.find(tag)
    return found.text.strip() if found is not None and found.text and found.text.strip() else None


def parse_xml(text: str) -> dict[str, Any] | None:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None

    items = [
        {
            "name": _text(it, "name") or _text(it, "item"),
            "quantity": _to_int(_text(it, "quantity") or _text(it, "qty")),
            "unit_price": parse_amount(_text(it, "unit_price") or _text(it, "price")),
            "amount": parse_amount(_text(it, "amount") or _text(it, "line_total")),
        }
        for it in (root.findall(".//line_items/item") or root.findall(".//item"))
    ]

    def field(tag: str) -> str | None:
        return _text(root, f".//{tag}")

    return _invoice(
        invoice_number=field("invoice_number"),
        vendor=field("vendor"),
        date=field("date") or field("invoice_date"),
        due_date=field("due_date"),
        currency=field("currency"),
        line_items=items,
        subtotal=parse_amount(field("subtotal")),
        tax=parse_amount(field("tax_amount") or field("tax")),
        tax_rate=parse_rate(field("tax_rate")),
        shipping=parse_amount(field("shipping")),
        total=parse_amount(field("total")),
        payment_terms=field("payment_terms"),
        notes=field("notes"),
    )
