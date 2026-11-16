"""Edge cases exactly on a threshold or stock limit, and the approver's read-only tools."""

import dataclasses

from invoice_pipeline.approval import build_tools
from invoice_pipeline.llm.base import run_tool
from invoice_pipeline.models import InvoiceData, LineItem
from invoice_pipeline.validation import Route, check_inventory, load_catalog, route


def _invoice(total=0.0, items=None):
    return InvoiceData(invoice_number="INV-B", vendor="Widgets Inc.", currency="USD", total=total, line_items=items or [])


def test_exactly_the_scrutiny_threshold_reaches_the_approver(settings):
    # At the threshold the invoice is scrutinized; a cent under it auto-approves.
    assert route(_invoice(total=settings.scrutiny_threshold), [], settings)[0] is Route.AGENT
    assert route(_invoice(total=settings.scrutiny_threshold - 0.01), [], settings)[0] is Route.AUTO_APPROVE


def test_cfo_threshold_is_an_open_bound(settings):
    # Exactly the CFO limit still goes to the agent; a cent over forces human review.
    assert route(_invoice(total=settings.cfo_threshold), [], settings)[0] is Route.AGENT
    assert route(_invoice(total=settings.cfo_threshold + 0.01), [], settings)[0] is Route.ESCALATE


def test_quantity_exactly_at_stock_is_not_overstock(conn):
    catalog = load_catalog(conn)  # WidgetA has 15 on hand
    at_limit = check_inventory(_invoice(items=[LineItem(name="WidgetA", quantity=15)]), catalog)
    assert at_limit == []
    over = check_inventory(_invoice(items=[LineItem(name="WidgetA", quantity=16)]), catalog)
    assert [f.code for f in over] == ["OVERSTOCK"]


def test_near_miss_item_name_does_not_fuzzy_match(conn):
    # WidgetC is one edit from WidgetA, but item lookup never fuzzy matches.
    catalog = load_catalog(conn)
    findings = check_inventory(_invoice(items=[LineItem(name="WidgetC", quantity=1)]), catalog)
    assert [f.code for f in findings] == ["UNKNOWN_ITEM"]


def test_approver_tools_are_read_only_and_answer_from_the_catalog(conn):
    tools = build_tools(conn)
    assert {t.name for t in tools} == {
        "get_catalog_item", "get_item_stock", "get_catalog_price", "get_vendor_history", "get_invoice_history",
    }
    assert run_tool(tools, "get_item_stock", {"item": "WidgetA"})["stock"] == 15
    assert run_tool(tools, "get_catalog_price", {"item": "WidgetA"})["unit_price"] == 250.0
    assert run_tool(tools, "get_catalog_price", {"item": "NotAThing"})["in_catalog"] is False
    # A rush-order qualifier resolves to the base catalog item.
    assert run_tool(tools, "get_item_stock", {"item": "WidgetA (rush order)"})["in_catalog"] is True


def test_invoice_history_tool_sees_prior_processing(conn, settings, run):
    run("invoice_1004.json")  # pay INV-1004 once so it is in the ledger
    history = run_tool(build_tools(conn), "get_invoice_history", {"invoice_number": "INV-1004"})
    assert history["prior_count"] == 1
    assert history["invoices"][0]["status"] == "paid"
