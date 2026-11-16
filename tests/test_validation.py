"""Validation rules, normalization, and payment safety."""

import dataclasses
import datetime as dt

from conftest import CLEAN_HIGH_VALUE

from invoice_pipeline.models import InvoiceData, LineItem, Outcome
from invoice_pipeline.normalize import (
    date_is_ambiguous,
    infer_currency,
    is_relative_date,
    number_key,
    parse_amount,
    parse_date,
    within_one_edit,
)
from invoice_pipeline.payment import pay
from invoice_pipeline.validation import (
    check_arithmetic,
    check_currency,
    check_dates,
    check_prices,
    check_pressure,
    check_tax,
    check_vendor,
    load_catalog,
    resolve_item,
)


def _invoice(**fields):
    base = {"invoice_number": "INV-1", "vendor": "Widgets Inc.", "currency": "USD"}
    return InvoiceData.model_validate(base | fields)


def test_normalization():
    assert number_key("INV 1012") == number_key("inv-1012") == number_key("INV-1O12") == "INV1012"
    assert parse_amount("$3,500.O0") == 3500.0
    assert within_one_edit("INV1004", "INV1040")
    assert not within_one_edit("INV1004", "INV2005")


def test_currency_inferred_from_dollar_sign(run):
    assert infer_currency("Total: $5,000.00") == "USD"
    assert infer_currency("Total: 5000") is None
    assert run("invoice_1010.txt").invoice.currency == "USD"


def test_foreign_currency_is_flagged(settings):
    assert [f.code for f in check_currency(_invoice(currency="EUR"), settings)] == ["FOREIGN_CURRENCY"]
    assert check_currency(_invoice(currency="USD"), settings) == []
    assert check_currency(_invoice(currency=None), settings) == []


def test_date_parsing_handles_sample_formats():
    assert parse_date("2026-01-20") == dt.date(2026, 1, 20)
    assert parse_date("Jan 30 2026") == dt.date(2026, 1, 30)
    assert parse_date("January 27, 2026") == dt.date(2026, 1, 27)
    assert parse_date("26-Jan-2O26") == dt.date(2026, 1, 26)  # OCR O read as zero
    assert parse_date("yesterday") is None
    assert is_relative_date("yesterday")
    assert date_is_ambiguous("03/04/2026")
    assert not date_is_ambiguous("01/28/2026")


def test_unknown_vendor_is_flagged(conn):
    assert [f.code for f in check_vendor(_invoice(vendor="Fraudster LLC"), conn)] == ["UNKNOWN_VENDOR"]
    assert check_vendor(_invoice(vendor="Widgets Inc."), conn) == []


def test_vendor_matches_on_primary_name_despite_a_rename_note(conn):
    # An approved vendor billing under a "(formerly ...)" note still matches.
    assert check_vendor(_invoice(vendor="QuickShip Distributers (formerly FastShip Ltd.)"), conn) == []
    # But an unapproved primary name does not, even if it claims to now be an approved one.
    assert [f.code for f in check_vendor(_invoice(vendor="FastShip Ltd. (now QuickShip Distributers)"), conn)] == ["UNKNOWN_VENDOR"]


def test_relative_due_date_escalates(settings):
    codes = {f.code for f in check_dates(_invoice(invoice_date="2026-01-20", due_date="yesterday"), settings)}
    assert "RELATIVE_DUE_DATE" in codes


def test_future_dated_invoice_is_flagged(settings):
    as_of = dataclasses.replace(settings, as_of_date=dt.date(2026, 1, 1))
    codes = {f.code for f in check_dates(_invoice(invoice_date="2026-06-01", due_date="2026-07-01"), as_of)}
    assert "FUTURE_DATED" in codes


def test_pressure_language_is_flagged(settings):
    noisy = _invoice(notes="URGENT - pay immediately via wire transfer to avoid penalties")
    assert [f.code for f in check_pressure(noisy, settings)] == ["PRESSURE_LANGUAGE"]
    assert check_pressure(_invoice(notes="Thank you for your business"), settings) == []


def test_tax_amount_checked_against_stated_rate(settings):
    assert check_tax(_invoice(subtotal=1000.0, tax=70.0, tax_rate=0.07), settings) == []
    bad = _invoice(subtotal=1000.0, tax=120.0, tax_rate=0.07)
    assert [f.code for f in check_tax(bad, settings)] == ["TAX_MISMATCH"]
    assert check_tax(_invoice(subtotal=1000.0, tax=70.0, tax_rate=None), settings) == []


def test_price_check_skipped_for_non_settleable_currency(conn, settings):
    catalog = load_catalog(conn)
    over = _invoice(currency="EUR", line_items=[LineItem(name="WidgetA", quantity=1, unit_price=9999.0)], total=9999.0)
    assert check_prices(over, catalog, settings) == []


def test_qualifier_is_stripped_only_on_exact_catalog_match(conn):
    catalog = load_catalog(conn)
    assert resolve_item("WidgetA (rush order)", catalog).name == "WidgetA"
    assert resolve_item("Widget A", catalog).name == "WidgetA"
    assert resolve_item("WidgetC", catalog) is None
    assert resolve_item("WidgetC (rush order)", catalog) is None


def test_small_unexplained_overcharge_is_flagged(settings):
    lines = [LineItem(name="WidgetA", quantity=10, unit_price=2251.28)]
    invoice = _invoice(line_items=lines, tax=0, total=22562.80)
    assert [f.code for f in check_arithmetic(invoice, settings)] == ["TOTAL_OVERCHARGE"]


def test_rounding_and_shipping_are_not_flagged(settings):
    lines = [LineItem(name="WidgetA", quantity=4, unit_price=250.0)]
    assert check_arithmetic(_invoice(line_items=lines, tax=82.5, total=1082.49), settings) == []
    assert check_arithmetic(_invoice(line_items=lines, tax=0, shipping=150, total=1150), settings) == []


def test_price_tolerance_is_asymmetric(conn, settings):
    catalog = load_catalog(conn)
    over = _invoice(line_items=[LineItem(name="WidgetA", quantity=1, unit_price=300.0)], total=300)
    under = _invoice(line_items=[LineItem(name="WidgetA", quantity=1, unit_price=240.0)], total=240)
    assert [f.code for f in check_prices(over, catalog, settings)] == ["PRICE_ABOVE_CATALOG"]
    assert check_prices(under, catalog, settings) == []


def test_rebill_under_new_number_escalates(run, write_invoice):
    run(write_invoice(CLEAN_HIGH_VALUE, "first.json"))
    state = run(write_invoice(CLEAN_HIGH_VALUE | {"invoice_number": "INV-7777"}, "second.json"))
    assert state.outcome is Outcome.ESCALATED
    assert "POSSIBLE_REBILL" in {f.code for f in state.findings}


def test_number_collision_escalates(run, write_invoice):
    run(write_invoice(CLEAN_HIGH_VALUE, "first.json"))
    other = CLEAN_HIGH_VALUE | {"vendor": {"name": "Atlas Industrial Supply"}, "total": 10750.0}
    state = run(write_invoice(other, "second.json"))
    assert "NUMBER_COLLISION" in {f.code for f in state.findings}


def test_payment_is_idempotent(conn):
    invoice = _invoice(line_items=[LineItem(name="WidgetA", quantity=1, unit_price=250.0)], total=250.0)
    calls = []

    def gateway(vendor, amount):
        calls.append((vendor, amount))
        return {"status": "success"}

    assert pay(conn, invoice, gateway)["replayed"] is False
    assert pay(conn, invoice, gateway)["replayed"] is True
    assert len(calls) == 1


def test_failed_payment_escalates_and_records_nothing(run, conn):
    state = run("invoice_1001.txt", gateway=lambda vendor, amount: {"status": "declined"})
    assert state.outcome is Outcome.ESCALATED
    assert "nothing was paid" in state.reasons[0]
    assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 0
