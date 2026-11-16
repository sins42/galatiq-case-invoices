"""Deterministic validation checks and the routing policy. No model calls here."""

from __future__ import annotations

import datetime as dt
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from . import db, ledger
from .config import Settings
from .models import Finding, InvoiceData, Severity
from .normalize import (
    date_is_ambiguous,
    is_relative_date,
    item_key,
    number_key,
    parse_date,
    strip_qualifier,
    vendor_key,
    within_one_edit,
)


class Route(str, Enum):
    REJECT = "reject"
    ESCALATE = "escalate"
    AUTO_APPROVE = "auto_approve"
    AGENT = "agent"


@dataclass(frozen=True)
class CatalogItem:
    name: str
    stock: int
    unit_price: float | None


Catalog = dict[str, CatalogItem]


def load_catalog(conn: sqlite3.Connection) -> Catalog:
    return {item_key(r["item"]): CatalogItem(r["item"], r["stock"], r["unit_price"]) for r in db.load_catalog(conn)}


def resolve_item(name: str, catalog: Catalog) -> CatalogItem | None:
    """Exact match after normalization. A trailing qualifier such as '(rush order)' is stripped,
    but only an exact catalog match after stripping counts."""
    return catalog.get(item_key(name)) or catalog.get(item_key(strip_qualifier(name)))


def _finding(code: str, severity: Severity, message: str, item: str | None = None) -> Finding:
    return Finding(code=code, severity=severity, message=message, item=item)


def _money(value: float) -> str:
    return f"{value:,.2f}"


def check_duplicates(invoice: InvoiceData, conn: sqlite3.Connection) -> list[Finding]:
    if not invoice.invoice_number:
        return []
    number, vendor = number_key(invoice.invoice_number), vendor_key(invoice.vendor)
    own_hash, own_bill = ledger.content_hash(invoice), ledger.bill_hash(invoice)
    findings: list[Finding] = []

    for prior in ledger.find_by_number(conn, number):
        if prior["vendor_key"] != vendor:
            findings.append(_finding(
                "NUMBER_COLLISION", Severity.REVIEW,
                f"Invoice number {invoice.invoice_number} was already used by {prior['vendor']}.",
            ))
        elif prior["content_hash"] == own_hash:
            return [_finding(
                "DUPLICATE_INVOICE", Severity.CRITICAL,
                f"Identical to {prior['invoice_number']} from {prior['source_file']}, already {prior['status']}.",
            )]
        else:
            delta = (invoice.total or 0) - (prior["total"] or 0)
            findings.append(_finding(
                "REVISION_OF_PRIOR", Severity.REVIEW,
                f"Changes {prior['invoice_number']} (previously {prior['status']}, total "
                f"{_money(prior['total'] or 0)}). New total {_money(invoice.total or 0)}, "
                f"difference {delta:+,.2f}.",
            ))

    for prior in ledger.find_by_bill(conn, own_bill, number):
        findings.append(_finding(
            "POSSIBLE_REBILL", Severity.REVIEW,
            f"Same vendor, items, and total as {prior['invoice_number']} ({prior['status']}, "
            f"recorded {prior['created_at']}) under a different number. Could be a rebill or a repeat order.",
        ))
        break

    # A near-identical number from the same vendor for the same amount is likely a mistyped duplicate.
    # Sequential numbers with different amounts are normal and not flagged.
    if invoice.total is not None:
        for prior in ledger.find_by_vendor_total(conn, vendor, invoice.total):
            other = prior["number_key"] or ""
            if other != number and prior["bill_hash"] != own_bill and within_one_edit(other, number):
                findings.append(_finding(
                    "NEAR_DUPLICATE_NUMBER", Severity.REVIEW,
                    f"Number {invoice.invoice_number} is one character from {prior['invoice_number']} "
                    f"with the same vendor and total.",
                ))
                break
    return findings


def check_completeness(invoice: InvoiceData) -> list[Finding]:
    findings = []
    if not invoice.vendor:
        findings.append(_finding("MISSING_VENDOR", Severity.CRITICAL, "No vendor on the invoice."))
    if invoice.total is None:
        findings.append(_finding("MISSING_TOTAL", Severity.CRITICAL, "No total on the invoice."))
    if not invoice.invoice_number:
        findings.append(_finding(
            "MISSING_INVOICE_NUMBER", Severity.REVIEW,
            "No invoice number, so it cannot be audited or checked for duplicates.",
        ))
    if not invoice.line_items:
        findings.append(_finding("NO_LINE_ITEMS", Severity.REVIEW, "No line items to check against inventory."))
    return findings


def check_integrity(invoice: InvoiceData) -> list[Finding]:
    findings = []
    for item in invoice.line_items:
        if item.quantity is None:
            findings.append(_finding("MISSING_QUANTITY", Severity.REVIEW, f"{item.name}: no quantity.", item.name))
        elif item.quantity <= 0:
            findings.append(_finding(
                "INVALID_QUANTITY", Severity.CRITICAL, f"{item.name}: quantity {item.quantity} is not positive.", item.name,
            ))
        if item.unit_price is not None and item.unit_price < 0:
            findings.append(_finding(
                "NEGATIVE_PRICE", Severity.CRITICAL, f"{item.name}: unit price {item.unit_price} is negative.", item.name,
            ))
    if invoice.total is not None and invoice.total < 0:
        findings.append(_finding("NEGATIVE_TOTAL", Severity.CRITICAL, f"Total {_money(invoice.total)} is negative."))
    return findings


def check_arithmetic(invoice: InvoiceData, settings: Settings) -> list[Finding]:
    if invoice.total is None or not invoice.line_items:
        return []
    findings: list[Finding] = []
    lines_total = 0.0
    for item in invoice.line_items:
        expected = item.quantity * item.unit_price if item.quantity is not None and item.unit_price is not None else None
        if item.amount is not None and expected is not None and abs(item.amount - expected) > 0.01:
            findings.append(_finding(
                "LINE_AMOUNT_MISMATCH", Severity.WARNING,
                f"{item.name}: {item.quantity} x {_money(item.unit_price)} is {_money(expected)}, "
                f"but the line says {_money(item.amount)}.",
                item.name,
            ))
        line = item.amount if item.amount is not None else expected
        if line is None:
            return findings + [_finding(
                "ARITHMETIC_UNVERIFIABLE", Severity.WARNING, f"{item.name}: no price or amount, total cannot be checked.",
            )]
        lines_total += line

    computed = lines_total + (invoice.tax or 0) + (invoice.shipping or 0) + (invoice.other_fees or 0)
    gap = invoice.total - computed
    tolerance = settings.arithmetic_tolerance
    if gap > tolerance.over_band(computed):
        findings.append(_finding(
            "TOTAL_OVERCHARGE", Severity.WARNING,
            f"Total {_money(invoice.total)} is {_money(gap)} more than line items plus tax and fees ({_money(computed)}).",
        ))
    elif -gap > tolerance.under_band(computed):
        findings.append(_finding(
            "TOTAL_SHORTFALL", Severity.WARNING,
            f"Total {_money(invoice.total)} is {_money(-gap)} less than line items plus tax and fees ({_money(computed)}).",
        ))
    return findings


def check_tax(invoice: InvoiceData, settings: Settings) -> list[Finding]:
    """When the invoice states a tax rate, verify the written tax amount matches rate times base."""
    if invoice.tax_rate is None or invoice.tax is None:
        return []
    base = invoice.subtotal
    if base is None:
        base = sum(
            item.amount if item.amount is not None else (item.quantity or 0) * (item.unit_price or 0)
            for item in invoice.line_items
        ) or None
    if base is None:
        return []
    expected = round(base * invoice.tax_rate, 2)
    tolerance = settings.arithmetic_tolerance
    gap = invoice.tax - expected
    if gap > tolerance.over_band(expected) or -gap > tolerance.under_band(expected):
        return [_finding(
            "TAX_MISMATCH", Severity.WARNING,
            f"Tax {_money(invoice.tax)} does not match the stated {invoice.tax_rate * 100:g}% rate on "
            f"{_money(base)} (expected {_money(expected)}).",
        )]
    return []


def check_inventory(invoice: InvoiceData, catalog: Catalog) -> list[Finding]:
    findings: list[Finding] = []
    demand: dict[str, int] = {}
    for item in invoice.line_items:
        entry = resolve_item(item.name, catalog)
        if entry is None:
            findings.append(_finding(
                "UNKNOWN_ITEM", Severity.REVIEW, f"'{item.name}' is not in the inventory catalog.", item.name,
            ))
            continue
        # Sum across lines so splitting an item over several lines cannot hide a shortage.
        demand[entry.name] = demand.get(entry.name, 0) + max(item.quantity or 0, 0)

    for name, quantity in demand.items():
        stock = catalog[item_key(name)].stock
        if stock == 0:
            findings.append(_finding("ZERO_STOCK", Severity.REVIEW, f"{name}: billed {quantity}, none in stock.", name))
        elif quantity > stock:
            findings.append(_finding(
                "OVERSTOCK", Severity.REVIEW, f"{name}: billed {quantity}, only {stock} in stock.", name,
            ))
    return findings


def check_currency(invoice: InvoiceData, settings: Settings) -> list[Finding]:
    if invoice.currency and invoice.currency.upper() not in settings.settleable_currencies:
        return [_finding(
            "FOREIGN_CURRENCY", Severity.REVIEW,
            f"Invoice is in {invoice.currency}, which the company does not settle directly.",
        )]
    return []


def check_prices(invoice: InvoiceData, catalog: Catalog, settings: Settings) -> list[Finding]:
    # Catalog prices are in the home currency. Comparing a foreign price to them without an
    # FX rate is meaningless, so skip the check; the currency flag escalates the invoice anyway.
    if invoice.currency and invoice.currency.upper() not in settings.settleable_currencies:
        return []
    findings: list[Finding] = []
    tolerance = settings.price_tolerance
    for item in invoice.line_items:
        entry = resolve_item(item.name, catalog)
        if entry is None or entry.unit_price is None or item.unit_price is None:
            continue
        diff = item.unit_price - entry.unit_price
        if diff > tolerance.over_band(entry.unit_price):
            findings.append(_finding(
                "PRICE_ABOVE_CATALOG", Severity.WARNING,
                f"{item.name}: billed {_money(item.unit_price)} per unit, catalog price is {_money(entry.unit_price)}.",
                item.name,
            ))
        elif -diff > tolerance.under_band(entry.unit_price):
            findings.append(_finding(
                "PRICE_BELOW_CATALOG", Severity.WARNING,
                f"{item.name}: billed {_money(item.unit_price)} per unit, catalog price is {_money(entry.unit_price)}.",
                item.name,
            ))
    return findings


def check_vendor(invoice: InvoiceData, conn: sqlite3.Connection) -> list[Finding]:
    if not invoice.vendor:
        return []  # completeness handles a missing vendor
    approved = {vendor_key(r["name"]) for r in conn.execute("SELECT name FROM vendors WHERE approved = 1").fetchall()}
    # Match on the primary name, stripping a trailing note such as "(formerly FastShip Ltd.)".
    if vendor_key(invoice.vendor) not in approved and vendor_key(strip_qualifier(invoice.vendor)) not in approved:
        return [_finding(
            "UNKNOWN_VENDOR", Severity.REVIEW, f"'{invoice.vendor}' is not an approved vendor.",
        )]
    return []


def check_dates(invoice: InvoiceData, settings: Settings) -> list[Finding]:
    findings: list[Finding] = []
    as_of = settings.as_of_date or dt.date.today()
    issue = parse_date(invoice.invoice_date)
    due = parse_date(invoice.due_date)

    if invoice.invoice_date is None:
        findings.append(_finding("MISSING_DATE", Severity.WARNING, "No invoice date."))
    if invoice.due_date is None:
        findings.append(_finding("MISSING_DUE_DATE", Severity.WARNING, "No due date."))
    elif is_relative_date(invoice.due_date):
        findings.append(_finding(
            "RELATIVE_DUE_DATE", Severity.REVIEW,
            f"Due date is written as '{invoice.due_date}', not a real date.",
        ))
    if issue and issue > as_of:
        findings.append(_finding(
            "FUTURE_DATED", Severity.REVIEW, f"Invoice date {invoice.invoice_date} is in the future.",
        ))
    if issue and due and due < issue:
        findings.append(_finding(
            "DUE_BEFORE_ISSUE", Severity.WARNING,
            f"Due date {invoice.due_date} is before the invoice date {invoice.invoice_date}.",
        ))
    if date_is_ambiguous(invoice.invoice_date):
        findings.append(_finding(
            "AMBIGUOUS_DATE", Severity.WARNING,
            f"Invoice date {invoice.invoice_date} could be read as day/month or month/day.",
        ))
    return findings


def check_pressure(invoice: InvoiceData, settings: Settings) -> list[Finding]:
    text = " ".join(
        part for part in [invoice.notes, invoice.payment_terms, *(li.note for li in invoice.line_items)] if part
    ).lower()
    # Match at a word boundary so "immediate" still catches "immediately" but "act now" does
    # not fire on "contact now".
    hits = sorted({phrase for phrase in settings.pressure_phrases if re.search(r"\b" + re.escape(phrase), text)})
    if hits:
        return [_finding(
            "PRESSURE_LANGUAGE", Severity.REVIEW,
            "Urgency or manipulation language in the free text: " + ", ".join(f"'{h}'" for h in hits) + ".",
        )]
    return []


def check_split_billing(invoice: InvoiceData, conn: sqlite3.Connection, settings: Settings) -> list[Finding]:
    total = invoice.total or 0
    if total <= 0 or total >= settings.scrutiny_threshold or not invoice.vendor:
        return []  # an invoice at the threshold already gets scrutiny on its own
    prior = ledger.recent_vendor_total(
        conn, invoice.vendor, settings.split_billing_window_days, number_key(invoice.invoice_number)
    )
    combined = prior + total
    if prior > 0 and combined >= settings.scrutiny_threshold:
        return [_finding(
            "SPLIT_BILLING", Severity.REVIEW,
            f"This invoice ({_money(total)}) plus recent invoices from {invoice.vendor} total "
            f"{_money(combined)}, over the scrutiny threshold of {_money(settings.scrutiny_threshold)}.",
        )]
    return []


def run_checks(invoice: InvoiceData, conn: sqlite3.Connection, settings: Settings) -> list[Finding]:
    catalog = load_catalog(conn)
    checks: list[Callable[[], list[Finding]]] = [
        lambda: check_duplicates(invoice, conn),
        lambda: check_completeness(invoice),
        lambda: check_integrity(invoice),
        lambda: check_arithmetic(invoice, settings),
        lambda: check_tax(invoice, settings),
        lambda: check_inventory(invoice, catalog),
        lambda: check_prices(invoice, catalog, settings),
        lambda: check_currency(invoice, settings),
        lambda: check_vendor(invoice, conn),
        lambda: check_dates(invoice, settings),
        lambda: check_pressure(invoice, settings),
        lambda: check_split_billing(invoice, conn, settings),
    ]
    return [finding for check in checks for finding in check()]


def route(invoice: InvoiceData, findings: list[Finding], settings: Settings) -> tuple[Route, list[str]]:
    """Decide what happens next. The model is only consulted for the AGENT route."""
    critical = [f.message for f in findings if f.severity is Severity.CRITICAL]
    if critical:
        return Route.REJECT, critical
    review = [f.message for f in findings if f.severity is Severity.REVIEW]
    if review:
        return Route.ESCALATE, review

    total = invoice.total or 0
    if total > settings.cfo_threshold:
        return Route.ESCALATE, [f"Total {_money(total)} is over the CFO limit of {_money(settings.cfo_threshold)}."]
    warnings = [f.message for f in findings if f.severity is Severity.WARNING]
    if warnings or total >= settings.scrutiny_threshold:
        reasons = warnings or [f"Total {_money(total)} is at or over {_money(settings.scrutiny_threshold)}."]
        return Route.AGENT, reasons
    return Route.AUTO_APPROVE, ["No findings and under the scrutiny threshold."]
