"""Domain models shared across the pipeline."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


def _blank_to_none(value: Any) -> Any:
    """Invoices use both empty strings and nulls for missing values. Treat them the same."""
    if isinstance(value, str) and not value.strip():
        return None
    return value


class Severity(str, Enum):
    CRITICAL = "critical"
    REVIEW = "review"
    WARNING = "warning"


class Outcome(str, Enum):
    PAID = "paid"
    REJECTED = "rejected"
    ESCALATED = "escalated"


class LineItem(BaseModel):
    name: str
    quantity: int | None = None
    unit_price: float | None = None
    amount: float | None = None
    note: str | None = None

    blank_to_none = field_validator("*", mode="before")(_blank_to_none)


class InvoiceData(BaseModel):
    """An invoice as written on the source document. Values are not corrected here."""

    invoice_number: str | None = None
    revision: str | None = None
    vendor: str | None = None
    invoice_date: str | None = None
    due_date: str | None = None
    currency: str | None = None
    line_items: list[LineItem] = Field(default_factory=list)
    subtotal: float | None = None
    tax: float | None = None
    tax_rate: float | None = None
    shipping: float | None = None
    other_fees: float | None = None
    total: float | None = None
    payment_terms: str | None = None
    notes: str | None = None

    blank_to_none = field_validator("*", mode="before")(_blank_to_none)

    @field_validator("line_items", mode="before")
    @classmethod
    def _none_to_empty(cls, value: Any) -> Any:
        return [] if value is None else value


class Finding(BaseModel):
    code: str
    severity: Severity
    message: str
    item: str | None = None


class ApprovalProposal(BaseModel):
    decision: Literal["approve", "escalate"]
    rationale: str


class CriticVerdict(BaseModel):
    verdict: Literal["accept", "object"]
    cited: str | None = None
    objection: str | None = None


class AuditEvent(BaseModel):
    timestamp: str
    stage: str
    outcome: str
    reason: str
    detail: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def now(cls, stage: str, outcome: str, reason: str, **detail: Any) -> AuditEvent:
        return cls(
            timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            stage=stage,
            outcome=outcome,
            reason=reason,
            detail=detail,
        )
