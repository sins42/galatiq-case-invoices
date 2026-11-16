"""Normalization rules used for matching, hashing, and grounding."""

from __future__ import annotations

import datetime as dt
import re

# Letters that OCR commonly confuses with digits.
_CONFUSABLES = str.maketrans({"o": "0", "l": "1", "i": "1"})
_QUALIFIER = re.compile(r"\s*\([^)]*\)\s*$")
_CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP"}

# Date words that are not real dates. A due date written like this needs review.
_RELATIVE_DATES = {
    "yesterday", "today", "tomorrow", "asap", "immediately", "now",
    "on receipt", "upon receipt", "due on receipt", "end of day", "eod",
}
_DATE_FORMATS = [
    "%Y-%m-%d", "%m/%d/%Y", "%b %d %Y", "%b %d, %Y", "%B %d %Y",
    "%B %d, %Y", "%d-%b-%Y", "%d %b %Y", "%d-%B-%Y",
]
_SLASH_DATE = re.compile(r"^\s*(\d{1,2})/(\d{1,2})/(\d{2,4})\s*$")


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


_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def parse_rate(value: str | float | None) -> float | None:
    """Normalize a tax rate to a fraction. '7%' and 'Tax (7%)' and 7 become 0.07; 0.07 stays 0.07."""
    if value is None:
        return None
    text = str(value)
    percent = _PERCENT.search(text)
    if percent:
        return float(percent.group(1)) / 100.0
    number = parse_amount(text)
    if number is None:
        return None
    return number / 100.0 if number > 1 else number


def fold(text: str) -> str:
    """Reduce text to a comparable form for grounding: lowercase, alphanumerics and dots only,
    OCR confusables mapped to digits. Applied to both sides of a comparison."""
    return re.sub(r"[^a-z0-9.]", "", text.lower()).translate(_CONFUSABLES)


def _fix_ocr_digits(text: str) -> str:
    """'2O26' -> '2026': map the letter O to zero when next to a digit."""
    return re.sub(r"(?<=\d)[Oo]|[Oo](?=\d)", "0", text)


def parse_date(text: str | None) -> dt.date | None:
    """Parse the date formats seen in the samples. Returns None if none match, so a parse
    failure never becomes a validation flag on its own."""
    if not text:
        return None
    cleaned = _fix_ocr_digits(text.strip())
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def is_relative_date(text: str | None) -> bool:
    """True for a word like 'yesterday' used in place of a real date."""
    return (text or "").strip().lower() in _RELATIVE_DATES


def date_is_ambiguous(text: str | None) -> bool:
    """A slash date like 03/04/2026 could be read day/month or month/day. 01/28/2026 cannot."""
    match = _SLASH_DATE.match(_fix_ocr_digits(text or ""))
    if not match:
        return False
    first, second = int(match.group(1)), int(match.group(2))
    return first <= 12 and second <= 12 and first != second


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
