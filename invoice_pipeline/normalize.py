"""Normalization rules used for matching, hashing, and grounding."""

from __future__ import annotations

import re

# Letters that OCR commonly confuses with digits.
_CONFUSABLES = str.maketrans({"o": "0", "l": "1", "i": "1"})
_QUALIFIER = re.compile(r"\s*\([^)]*\)\s*$")
_CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP"}


def infer_currency(text: str) -> str | None:
    """Map a currency symbol in the text to its ISO code. A dollar sign is USD for this US company."""
    for symbol, code in _CURRENCY_SYMBOLS.items():
        if symbol in text:
            return code
    return None


def item_key(name: str) -> str:
    """'Widget A' and 'widgeta' compare equal. No fuzzy matching."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def strip_qualifier(name: str) -> str:
    """'WidgetA (rush order)' -> 'WidgetA'."""
    return _QUALIFIER.sub("", name)


def vendor_key(vendor: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (vendor or "").lower())


def number_key(invoice_number: str | None) -> str:
    """Canonical invoice number: 'INV 1012', 'inv-1012', and 'INV-1O12' all become 'INV1012'."""
    text = (invoice_number or "").upper()
    text = re.sub(r"(?<=\d)O|O(?=\d)", "0", text)
    return re.sub(r"[^A-Z0-9]", "", text)


def parse_amount(text: str | None) -> float | None:
    """Parse '$3,500.O0' as 3500.0. Returns None when there is no number."""
    if text is None:
        return None
    cleaned = re.sub(r"(?<=[\d.,])[Oo]|[Oo](?=[\d.,])", "0", text)
    cleaned = cleaned.replace("$", "").replace(",", "").strip()
    match = re.search(r"-?\d+(?:\.\d+)?", cleaned)
    return float(match.group()) if match else None


def fold(text: str) -> str:
    """Reduce text to a comparable form for grounding: lowercase, alphanumerics and dots only,
    OCR confusables mapped to digits. Applied to both sides of a comparison."""
    return re.sub(r"[^a-z0-9.]", "", text.lower()).translate(_CONFUSABLES)


def within_one_edit(a: str, b: str) -> bool:
    """True if a and b differ by at most one insertion, deletion, substitution, or adjacent swap."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diffs = [i for i in range(len(a)) if a[i] != b[i]]
        if len(diffs) == 1:
            return True
        return len(diffs) == 2 and diffs[1] == diffs[0] + 1 and a[diffs[0]] == b[diffs[1]] and a[diffs[1]] == b[diffs[0]]
    shorter, longer = (a, b) if len(a) < len(b) else (b, a)
    return any(longer[:i] + longer[i + 1 :] == shorter for i in range(len(longer)))
