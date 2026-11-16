"""The LangGraph workflow: ingest, extract, validate, approve, critique, pay, finalize."""

from __future__ import annotations

import functools
import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from langgraph.graph import END, START, StateGraph

from . import approval, audit, ledger, payment, readers, validation
from .config import Settings
from .extraction import apply_home_currency, from_structured, model_attempt, source_text
from .llm import Provider
from .logs import log_event
from .models import Outcome
from .payment import Gateway, mock_payment
from .state import PipelineState
from .validation import Route

# Route values that are not validation routes.
EXTRACT, CRITIQUE, APPROVE, PAY, PAID = "extract", "critique", "approve", "pay", "paid"

Node = Callable[..., dict[str, Any]]


def _guarded(stage: str) -> Callable[[Node], Node]:
    """Any unexpected error escalates the invoice. The pipeline never fails into a paid state."""

    def decorate(node: Node) -> Node:
        @functools.wraps(node)
        def wrapper(*args: Any) -> dict[str, Any]:
            state: PipelineState = args[-1]
            try:
                return node(*args)
            except Exception as exc:
                reason = f"{stage} failed: {exc}"
                log_event("stage_error", logging.ERROR, stage=stage, error=str(exc), file=state.invoice_path)
                return {
                    "route": Route.ESCALATE.value,
                    "reasons": [reason],
                    "audit": [audit.event(stage, "error", reason)],
                }

        return wrapper

    return decorate


class InvoicePipeline:
    def __init__(
        self,
        settings: Settings,
        provider: Provider,
        conn: sqlite3.Connection,
        gateway: Gateway = mock_payment,
    ):
        self.settings = settings
        self.provider = provider
        self.conn = conn
        self.gateway = gateway
        self.tools = approval.build_tools(conn)
        self.graph = self._build()

    def run(self, invoice_path: Path | str, run_id: str) -> PipelineState:
        initial = PipelineState(run_id=run_id, invoice_path=str(invoice_path), provider=self.provider.name)
        return PipelineState.model_validate(self.graph.invoke(initial))

    def _build(self):
        graph = StateGraph(PipelineState)
        graph.add_node("ingest", self.ingest)
        graph.add_node("extract", self.extract)
        graph.add_node("validate", self.validate)
        graph.add_node("approve", self.approve)
        graph.add_node("critique", self.critique)
        graph.add_node("pay", self.pay)
        graph.add_node("finalize", self.finalize)

        graph.add_edge(START, "ingest")
        graph.add_conditional_edges("ingest", self._next_or_finalize, ["extract", "finalize"])
        graph.add_conditional_edges("extract", self._next_or_finalize, ["extract", "validate", "finalize"])
        graph.add_conditional_edges("validate", self._after_validate, ["approve", "pay", "finalize"])
        graph.add_conditional_edges("approve", self._next_or_finalize, ["critique", "finalize"])
        graph.add_conditional_edges("critique", self._next_or_finalize, ["approve", "pay", "finalize"])
        graph.add_edge("pay", "finalize")
        graph.add_edge("finalize", END)
        return graph.compile()

    # Routers

    @staticmethod
    def _next_or_finalize(state: PipelineState) -> str:
        return state.route if state.route in {EXTRACT, "validate", APPROVE, CRITIQUE, PAY} else "finalize"

    @staticmethod
    def _after_validate(state: PipelineState) -> str:
        if state.route == Route.AGENT.value:
            return APPROVE
        if state.route == Route.AUTO_APPROVE.value:
            return PAY
        return "finalize"

    # Nodes

    @_guarded("ingest")
    def ingest(self, state: PipelineState) -> dict[str, Any]:
        try:
            file_format, content = readers.load(Path(state.invoice_path))
        except readers.IngestionError as exc:
            log_event("ingest_failed", logging.WARNING, file=state.invoice_path, error=str(exc))
            return {
                "route": Route.ESCALATE.value,
                "reasons": [str(exc)],
                "audit": [audit.event("ingest", "escalate", str(exc))],
            }
        log_event("ingested", file=state.invoice_path, format=file_format)
        return {
            "file_format": file_format,
            "raw_content": content,
            "route": EXTRACT,
            "audit": [audit.event("ingest", "loaded", f"Read {file_format} file", format=file_format)],
        }

    @_guarded("extract")
    def extract(self, state: PipelineState) -> dict[str, Any]:
        structured = state.file_format in readers.STRUCTURED_FORMATS and not isinstance(state.raw_content, str)
        if state.method is None and structured:
            invoice = from_structured(state.raw_content)
            if invoice is not None:
                apply_home_currency(invoice, self.settings.home_currency)
                log_event("extracted", file=state.invoice_path, method="structured")
                return {
                    "invoice": invoice,
                    "method": "structured",
                    "route": "validate",
                    "audit": [audit.event("extract", "extracted", "Parsed directly, no model call", method="structured")],
                }

        attempts = state.extraction_attempts + 1
        attempt = model_attempt(self.provider, source_text(state.raw_content), state.last_output, state.extraction_error)
        detail = {"attempt": attempts, "prompt": attempt.prompt, "response": attempt.raw_output, "error": attempt.error}

        if attempt.invoice is not None:
            apply_home_currency(attempt.invoice, self.settings.home_currency)
            log_event("extracted", file=state.invoice_path, method="model", attempts=attempts)
            return {
                "invoice": attempt.invoice,
                "method": "model",
                "extraction_attempts": attempts,
                "route": "validate",
                "audit": [audit.event("extract", "extracted", f"Model extraction passed on attempt {attempts}", **detail)],
            }

        if attempts > self.settings.extraction_max_retries:
            reason = f"Could not extract the invoice after {attempts} attempts. Last error: {attempt.error}"
            log_event("extraction_failed", logging.WARNING, file=state.invoice_path, attempts=attempts)
            return {
                "method": "model",
                "extraction_attempts": attempts,
                "route": Route.ESCALATE.value,
                "reasons": [reason],
                "audit": [audit.event("extract", "escalate", reason, **detail)],
            }

        log_event("extraction_retry", logging.WARNING, file=state.invoice_path, attempt=attempts, error=attempt.error)
        return {
            "method": "model",
            "extraction_attempts": attempts,
            "extraction_error": attempt.error,
            "last_output": attempt.raw_output,
            "route": EXTRACT,
            "audit": [audit.event("extract", "retry", "Output failed a check, retrying with the error", **detail)],
        }

    @_guarded("validate")
    def validate(self, state: PipelineState) -> dict[str, Any]:
        findings = validation.run_checks(state.invoice, self.conn, self.settings)
        next_route, reasons = validation.route(state.invoice, findings, self.settings)
        log_event("validated", file=state.invoice_path, route=next_route.value, findings=[f.code for f in findings])
        return {
            "findings": findings,
            "route": next_route.value,
            "reasons": reasons,
            "audit": [
                audit.event(
                    "validate", next_route.value, "; ".join(reasons),
                    findings=[f.model_dump(mode="json") for f in findings],
                )
            ],
        }

    @_guarded("approve")
    def approve(self, state: PipelineState) -> dict[str, Any]:
        context = approval.approval_context(state.invoice, state.findings, self.settings)
        proposal = approval.propose(self.provider, self.tools, context, state.critic_feedback)
        detail = {"rationale": proposal.rationale, "tool_calls": proposal.tool_calls, "response": proposal.raw}
        log_event("approval_proposed", file=state.invoice_path, decision=proposal.decision, tools=len(proposal.tool_calls))
        update: dict[str, Any] = {
            "proposal": {"decision": proposal.decision, "rationale": proposal.rationale, "tool_calls": proposal.tool_calls},
            "audit": [audit.event("approve", proposal.decision, proposal.rationale, **detail)],
        }
        if proposal.decision == "escalate":
            return {**update, "route": Route.ESCALATE.value, "reasons": [f"Approver escalated: {proposal.rationale}"]}
        return {**update, "route": CRITIQUE}

    @_guarded("critique")
    def critique(self, state: PipelineState) -> dict[str, Any]:
        context = approval.approval_context(state.invoice, state.findings, self.settings)
        proposal = approval.Proposal(
            decision=state.proposal["decision"],
            rationale=state.proposal["rationale"],
            tool_calls=state.proposal.get("tool_calls", []),
        )
        verdict = approval.critique(self.provider, context, proposal)
        rounds = state.critic_rounds + 1
        log_event("critique", file=state.invoice_path, verdict=verdict.verdict, round=rounds)
        event_detail = {"round": rounds, "cited": verdict.cited, "objection": verdict.objection}

        if verdict.verdict == "accept":
            # Policy gate: the final decision is the more cautious of the policy and the agent.
            if (state.invoice.total or 0) > self.settings.cfo_threshold:
                reason = "Approval overridden: total is over the CFO limit."
                return {"critic_rounds": rounds, "route": Route.ESCALATE.value, "reasons": [reason],
                        "audit": [audit.event("critique", "escalate", reason, **event_detail)]}
            reason = f"Approved after review: {proposal.rationale}"
            return {"critic_rounds": rounds, "route": PAY, "reasons": [reason],
                    "audit": [audit.event("critique", "accept", reason, **event_detail)]}

        objection = f"[{verdict.cited}] {verdict.objection}"
        if rounds >= self.settings.critic_max_rounds:
            reason = f"Approver and reviewer did not agree after {rounds} rounds. Last objection: {objection}"
            return {"critic_rounds": rounds, "route": Route.ESCALATE.value, "reasons": [reason],
                    "audit": [audit.event("critique", "escalate", reason, **event_detail)]}
        return {"critic_rounds": rounds, "critic_feedback": objection, "route": APPROVE,
                "audit": [audit.event("critique", "object", objection, **event_detail)]}

    @_guarded("pay")
    def pay(self, state: PipelineState) -> dict[str, Any]:
        try:
            result = payment.pay(self.conn, state.invoice, self.gateway)
        except Exception as exc:
            result = {"status": "error", "detail": str(exc)}
        if result.get("status") != "success":
            reason = f"Payment failed, nothing was paid: {result.get('detail') or result.get('status')}"
            log_event("payment_failed", logging.ERROR, file=state.invoice_path, result=result)
            return {"payment": result, "route": Route.ESCALATE.value, "reasons": [reason],
                    "audit": [audit.event("pay", "escalate", reason, result=result)]}
        note = "Already paid, no second payment" if result.get("replayed") else "Paid"
        log_event("paid", file=state.invoice_path, reference=result["reference"], replayed=result["replayed"])
        return {"payment": result, "outcome": Outcome.PAID, "route": PAID,
                "audit": [audit.event("pay", "paid", note, result=result)]}

    def finalize(self, state: PipelineState) -> dict[str, Any]:
        if state.outcome is Outcome.PAID:
            outcome = Outcome.PAID
        elif state.route == Route.REJECT.value:
            outcome = Outcome.REJECTED
        else:
            outcome = Outcome.ESCALATED

        ledger.record(
            self.conn,
            run_id=state.run_id,
            source_file=Path(state.invoice_path).name,
            invoice=state.invoice,
            outcome=outcome,
            findings=state.findings,
            reasons=state.reasons,
            payment_ref=(state.payment or {}).get("reference") if outcome is Outcome.PAID else None,
            file_format=state.file_format,
            method=state.method,
            source_text=source_text(state.raw_content) if state.raw_content is not None else None,
            approval="agent_escalated" if state.proposal else "skipped",
        )
        final_event = audit.event("finalize", outcome.value, "; ".join(state.reasons))
        record = {
            "run_id": state.run_id,
            "source_file": state.invoice_path,
            "provider": state.provider,
            "outcome": outcome.value,
            "reasons": state.reasons,
            "invoice": state.invoice.model_dump(mode="json") if state.invoice else None,
            "findings": [f.model_dump(mode="json") for f in state.findings],
            "payment": state.payment,
            "audit": [e.model_dump(mode="json") for e in [*state.audit, final_event]],
        }
        try:
            audit_file = str(audit.write_record(self.settings.runs_dir, state.run_id, state.invoice_path, record))
        except OSError as exc:
            # The ledger record above is the durable outcome. A failed audit file must not crash the run.
            audit_file = None
            log_event("audit_write_failed", logging.ERROR, file=state.invoice_path, error=str(exc))
        log_event("finalized", file=state.invoice_path, outcome=outcome.value, audit_file=audit_file)
        return {"outcome": outcome, "audit_file": audit_file, "audit": [final_event]}
