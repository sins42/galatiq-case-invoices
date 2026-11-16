"""Turn loaded content into InvoiceData.

Structured files are mapped directly. Text, and structured files that do not fit the schema,
go to the model. Model output is checked against the schema and grounded in the source text.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from . import prompts
from .llm import LLMRequest, Provider, parse_json_object
from .models import InvoiceData
from .normalize import fold, infer_currency, parse_rate


def from_structured(data: Any) -> InvoiceData | None:
    """Map a parsed JSON invoice onto InvoiceData. Returns None if it does not fit."""
    if not isinstance(data, dict) or not isinstance(data.get("line_items"), list):
        return None
    vendor = data.get("vendor")
    if isinstance(vendor, dict):
        vendor = vendor.get("name")
    try:
        return InvoiceData.model_validate(
            {
                "invoice_number": data.get("invoice_number"),
                "revision": data.get("revision"),
                "vendor": vendor,
                "invoice_date": data.get("date") or data.get("invoice_date"),
                "due_date": data.get("due_date"),
                "currency": data.get("currency"),
                "line_items": [
                    {
                        "name": li.get("item") or li.get("name"),
                        "quantity": li.get("quantity"),
                        "unit_price": li.get("unit_price"),
                        "amount": li.get("amount"),
                        "note": li.get("note"),
                    }
                    for li in data["line_items"]
                ],
                "subtotal": data.get("subtotal"),
                "tax": data.get("tax_amount", data.get("tax")),
                "tax_rate": parse_rate(data.get("tax_rate")),
                "shipping": data.get("shipping"),
                "total": data.get("total"),
                "payment_terms": data.get("payment_terms"),
                "notes": data.get("notes"),
            }
        )
    except (ValidationError, AttributeError, TypeError):
        return None


def source_text(content: Any) -> str:
    return content if isinstance(content, str) else json.dumps(content, indent=2)


# A field label at the start of a line that names an invoice total. Labels are often abbreviated,
# so "Amt" and "Amount Due" count as well as "Total".
_TOTAL_LABEL = re.compile(
    r"^\s*(?:grand\s+total|invoice\s+total|total\s+amount|total\s+due|amount\s+due|balance\s+due|total|balance|amount|amt|sum)\b",
    re.IGNORECASE,
)
# A currency-looking amount: a $ figure, a two-decimal number, or a thousands-separated number.
# This is deliberately stricter than any digit so a bare count like "qty 20" is not read as money.
_MONEY = re.compile(r"\$\s*\d[\d,]*(?:\.\d+)?|\d[\d,]*\.\d{2}|\d{1,3}(?:,\d{3})+")


def stated_total(text: str) -> str | None:
    """The source line stating a total, if one is present. Used to prompt a retry when the model
    returns a null total although the invoice clearly states one under an abbreviated label."""
    for line in text.splitlines():
        if _TOTAL_LABEL.match(line) and _MONEY.search(line):
            return line.strip()
    return None


def apply_home_currency(invoice: InvoiceData, home_currency: str) -> InvoiceData:
    """An invoice with no stated currency is assumed to be in the company's home currency.
    Prices are checked against the home-currency catalog, so this is the safe assumption."""
    if invoice.currency is None:
        invoice.currency = home_currency
    return invoice


def _number_forms(value: float) -> list[str]:
    value = abs(value)
    forms = [f"{value:.2f}"]
    if value == int(value):
        forms.append(str(int(value)))
    return forms


def ungrounded_values(invoice: InvoiceData, text: str) -> list[str]:
    """Values the model returned that do not appear in the source text."""
    source = fold(text)
    problems: list[str] = []

    def check_text(label: str, value: str | None) -> None:
        if value and fold(value) not in source:
            problems.append(f"{label} '{value}' does not appear in the invoice")

    def check_number(label: str, value: float | None) -> None:
        if value is not None and not any(form in source for form in _number_forms(value)):
            problems.append(f"{label} {value} does not appear in the invoice")

    check_text("invoice_number", invoice.invoice_number)
    check_text("vendor", invoice.vendor)
    check_text("invoice_date", invoice.invoice_date)
    check_text("due_date", invoice.due_date)
    for i, item in enumerate(invoice.line_items):
        check_text(f"line_items[{i}].name", item.name)
        check_number(f"line_items[{i}].quantity", item.quantity)
        check_number(f"line_items[{i}].unit_price", item.unit_price)
        check_number(f"line_items[{i}].amount", item.amount)
    for field in ("subtotal", "tax", "shipping", "total"):
        check_number(field, getattr(invoice, field))
    return problems


@dataclass
class Attempt:
    invoice: InvoiceData | None
    raw_output: str
    error: str | None
    prompt: str


def model_attempt(provider: Provider, text: str, previous_output: str | None, error: str | None) -> Attempt:
    """One pass of the verifier loop: call the model, then check schema and grounding."""
    prompt = prompts.extraction_prompt(text, previous_output, error)
    request = LLMRequest(
        task="extract",
        role="extraction",
        system=prompts.EXTRACTION_SYSTEM,
        user=prompt,
        context={"source_text": text},
    )
    raw = provider.complete(request).text
    try:
        invoice = InvoiceData.model_validate(parse_json_object(raw))
    except (ValueError, ValidationError) as exc:
        return Attempt(None, raw, f"Output does not match the schema: {exc}", prompt)
    problems = ungrounded_values(invoice, text)
    if problems:
        return Attempt(None, raw, "Values not found in the invoice: " + "; ".join(problems), prompt)
    if invoice.total is None:
        stated = stated_total(text)
        if stated is not None:
            error = (
                f"The invoice states a total on this line but you left total null: {stated!r}. "
                "The total may be labeled with an abbreviation such as 'Amt' or 'Amount Due'. "
                "Extract that amount into the total field."
            )
            return Attempt(None, raw, error, prompt)
    if invoice.currency is None:
        invoice.currency = infer_currency(text)
    if invoice.tax_rate is not None:
        invoice.tax_rate = parse_rate(invoice.tax_rate)
    return Attempt(invoice, raw, None, prompt)
