"""The LangGraph state carried through every stage."""

from __future__ import annotations

import operator
from typing import Annotated, Any

from pydantic import BaseModel, Field

from .models import AuditEvent, Finding, InvoiceData, Outcome


class PipelineState(BaseModel):
    run_id: str
    invoice_path: str

    # Ingestion
    file_format: str | None = None
    raw_content: Any = None

    # Extraction
    method: str | None = None
    invoice: InvoiceData | None = None
    extraction_attempts: int = 0
    extraction_error: str | None = None
    last_output: str | None = None

    # Validation and routing. `route` is the next step, or the terminal decision.
    findings: list[Finding] = Field(default_factory=list)
    route: str | None = None
    reasons: list[str] = Field(default_factory=list)

    # Approval
    proposal: dict[str, Any] | None = None
    critic_rounds: int = 0
    critic_feedback: str | None = None

    # Payment and result
    payment: dict[str, Any] | None = None
    outcome: Outcome | None = None
    provider: str | None = None
    audit_file: str | None = None

    # Append-only: every stage adds events and none overwrite earlier ones.
    audit: Annotated[list[AuditEvent], operator.add] = Field(default_factory=list)
