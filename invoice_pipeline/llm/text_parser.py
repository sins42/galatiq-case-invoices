"""Rule-based extraction for text invoices, used by the offline provider.

It covers the layouts seen in the sample data: labeled fields, abbreviated headers,
emails, and column tables. Values are returned as written, like the model path.
"""

from __future__ import annotations

import re
from typing import Any

from ..normalize import infer_currency, parse_amount, parse_rate

_FLAGS = re.MULTILINE | re.IGNORECASE

_NUMBER_PATTERNS = [
    r"^\s*Invoice Number\s*:\s*(.+?)\s*$",
    r"^\s*INV NO\s*:\s*(.+?)\s*$",
    r"^\s*Inv\s*#\s*:\s*(.+?)\s*$",
    r"^\s*Invoice\s*:\s*(.+?)\s*$",
    r"INVOICE\s*#\s*([A-Z0-9-]+)",
]
_VENDOR_PATTERNS = [r"^\s*(?:Vendor|Vndr)\s*:\s*(.+?)\s*$", r"^\s*FROM\s*:\s*(.+?)\s*$"]
_DATE_PATTERNS = [r"^\s*(?:Date|Dt)\s*:\s*(.+?)\s*$"]
_DUE_PATTERNS = [r"^\s*(?:Due Date|Due Dt|Due)\s*:\s*(.+?)\s*$"]
_TERMS_PATTERNS = [r"^\s*(?:Payment Terms|Pymnt Terms|Terms)\s*:\s*(.+?)\s*$"]
_NOTES_PATTERNS = [r"^\s*Notes?\s*:\s*(.+?)\s*$"]

_AMOUNT = r"\$?\s*(-?[\d,]+(?:\.[\dOo]{1,2})?)"
_SUBTOTAL = rf"^\s*Sub\s*total\s*:\s*{_AMOUNT}"
_TAX = rf"^\s*(?:Sales\s+)?Tax[^:\n]*:\s*{_AMOUNT}"
_TAX_RATE = re.compile(r"^\s*(?:Sales\s+)?Tax[^:\n]*?(\d+(?:\.\d+)?)\s*%", _FLAGS)
_SHIPPING = rf"^\s*Shipping[^:\n]*:\s*{_AMOUNT}"
_TOTAL = rf"^\s*(?:Total Amount|Grand Total|Total|Amt)\s*:\s*{_AMOUNT}"

# A line item: a name, a quantity, a unit price, an optional line amount, and an optional note.
_LINE = re.compile(
    r"^\s*-?\s*(?P<name>[A-Za-z][A-Za-z0-9 ]*?(?:\s*\([^)]*\))?)\s+"
    r"(?:qty:?\s*|x)?(?P<qty>-?\d+)\s+"
    r"(?:unit price:\s*|@\s*)?\$(?P<price>[\d,]+(?:\.[\dOo]{1,2})?)"
    r"(?:\s+(?:ea|each))?"
    r"(?:\s+\$(?P<amount>[\d,]+(?:\.[\dOo]{1,2})?))?"
    r"(?:\s+(?P<note>\S.*?))?\s*$",
    re.IGNORECASE,
)


def _first(patterns: list[str], text: str) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text, _FLAGS)
        if match:
            return match.group(1).strip()
    return None


def _amount(pattern: str, text: str) -> float | None:
    match = re.search(pattern, text, _FLAGS)
    return parse_amount(match.group(1)) if match else None


def _split_columns(text: str) -> str:
    """A wide gap (3+ spaces) separates columns, as in a two-column PDF header. Split each
    column onto its own line so labeled fields are read one at a time. Line items use single
    spaces, so they are read from the original text and are unaffected."""
    lines: list[str] = []
    for line in text.splitlines():
        lines.extend(re.split(r"\s{3,}", line))
    return "\n".join(lines)


def parse_text_invoice(text: str) -> dict[str, Any]:
    fields = _split_columns(text)
    line_items = []
    for line in text.splitlines():
        match = _LINE.match(line)
        if match:
            line_items.append(
                {
                    "name": match["name"].strip(),
                    "quantity": int(match["qty"]),
                    "unit_price": parse_amount(match["price"]),
                    "amount": parse_amount(match["amount"]) if match["amount"] else None,
                    "note": match["note"].strip() if match["note"] else None,
                }
            )

    rate_match = _TAX_RATE.search(fields)
    return {
        "invoice_number": _first(_NUMBER_PATTERNS, fields),
        "vendor": _first(_VENDOR_PATTERNS, fields),
        "invoice_date": _first(_DATE_PATTERNS, fields),
        "due_date": _first(_DUE_PATTERNS, fields),
        "currency": infer_currency(text),
        "line_items": line_items,
        "subtotal": _amount(_SUBTOTAL, fields),
        "tax": _amount(_TAX, fields),
        "tax_rate": parse_rate(rate_match.group(0)) if rate_match else None,
        "shipping": _amount(_SHIPPING, fields),
        "total": _amount(_TOTAL, fields),
        "payment_terms": _first(_TERMS_PATTERNS, fields),
        "notes": _first(_NOTES_PATTERNS, fields),
    }
