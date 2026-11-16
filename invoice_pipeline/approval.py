"""The approval agent: an actor that proposes a decision and a critic that checks it."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from . import ledger, prompts
from .config import Settings
from .llm import LLMRequest, Provider, Tool, parse_json_object
from .models import ApprovalProposal, CriticVerdict, Finding, InvoiceData
from .normalize import number_key
from .validation import load_catalog, resolve_item

_ITEM_PARAM = {"type": "object", "properties": {"item": {"type": "string"}}, "required": ["item"]}
_VENDOR_PARAM = {"type": "object", "properties": {"vendor": {"type": "string"}}, "required": ["vendor"]}
_NUMBER_PARAM = {"type": "object", "properties": {"invoice_number": {"type": "string"}}, "required": ["invoice_number"]}


def build_tools(conn: sqlite3.Connection) -> list[Tool]:
    """Read-only lookups. None of them can approve or pay."""

    def get_catalog_item(item: str) -> dict[str, Any]:
        entry = resolve_item(item, load_catalog(conn))
        if entry is None:
            return {"item": item, "in_catalog": False}
        return {"item": entry.name, "in_catalog": True, "stock": entry.stock, "unit_price": entry.unit_price}

    def get_item_stock(item: str) -> dict[str, Any]:
        entry = resolve_item(item, load_catalog(conn))
        return {"item": item, "in_catalog": entry is not None, "stock": entry.stock if entry else None}

    def get_catalog_price(item: str) -> dict[str, Any]:
        entry = resolve_item(item, load_catalog(conn))
        return {"item": item, "in_catalog": entry is not None, "unit_price": entry.unit_price if entry else None}

    def get_vendor_history(vendor: str) -> dict[str, Any]:
        rows = ledger.vendor_history(conn, vendor)
        return {
            "vendor": vendor,
            "invoice_count": len(rows),
            "combined_total": round(sum(r["total"] or 0 for r in rows), 2),
            "invoices": [dict(r) for r in rows],
        }

    def get_invoice_history(invoice_number: str) -> dict[str, Any]:
        rows = ledger.find_by_number(conn, number_key(invoice_number))
        return {
            "invoice_number": invoice_number,
            "prior_count": len(rows),
            "invoices": [
                {"invoice_number": r["invoice_number"], "total": r["total"], "status": r["status"]} for r in rows
            ],
        }

    return [
        Tool("get_catalog_item", "Look up an item's stock level and catalog unit price.", _ITEM_PARAM, get_catalog_item),
        Tool("get_item_stock", "Look up only an item's stock level on hand.", _ITEM_PARAM, get_item_stock),
        Tool("get_catalog_price", "Look up only an item's agreed catalog unit price.", _ITEM_PARAM, get_catalog_price),
        Tool("get_vendor_history", "List the vendor's recently processed invoices and their combined total.", _VENDOR_PARAM, get_vendor_history),
        Tool("get_invoice_history", "List prior processing of this invoice number, to spot revisions and re-bills.", _NUMBER_PARAM, get_invoice_history),
    ]


def approval_context(invoice: InvoiceData, findings: list[Finding], settings: Settings) -> dict[str, Any]:
    fields = invoice.model_dump(mode="json", exclude={"notes"})
    return {
        "invoice": fields,
        "findings": [f.model_dump(mode="json") for f in findings],
        "notes": invoice.notes,
        "scrutiny_threshold": settings.scrutiny_threshold,
        "strict": (invoice.total or 0) >= settings.scrutiny_threshold,
    }


@dataclass
class Proposal:
    decision: str
    rationale: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    raw: str = ""


def propose(provider: Provider, tools: list[Tool], context: dict[str, Any], feedback: str | None) -> Proposal:
    response = provider.complete(
        LLMRequest(
            task="approve",
            role="approval",
            system=prompts.APPROVER_SYSTEM,
            user=prompts.approval_prompt(context, feedback),
            context={**context, "feedback": feedback},
            tools=tools,
        )
    )
    try:
        parsed = ApprovalProposal.model_validate(parse_json_object(response.text))
    except (ValueError, ValidationError):
        return Proposal("escalate", "The approver returned an unreadable answer.", response.tool_calls, response.text)
    return Proposal(parsed.decision, parsed.rationale, response.tool_calls, response.text)


def critique(provider: Provider, context: dict[str, Any], proposal: Proposal) -> CriticVerdict:
    """Review a drafted approval. An objection without a citation is treated as acceptance."""
    proposed = {"decision": proposal.decision, "rationale": proposal.rationale}
    response = provider.complete(
        LLMRequest(
            task="critique",
            role="approval",
            system=prompts.CRITIC_SYSTEM,
            user=prompts.critic_prompt(context, proposed, proposal.tool_calls),
            context={**context, "proposal": proposed},
        )
    )
    try:
        verdict = CriticVerdict.model_validate(parse_json_object(response.text))
    except (ValueError, ValidationError):
        return CriticVerdict(verdict="object", cited="unreadable", objection="The critic returned an unreadable answer.")
    if verdict.verdict == "object" and not (verdict.cited or "").strip():
        return CriticVerdict(verdict="accept")
    return verdict
