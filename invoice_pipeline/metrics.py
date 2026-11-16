"""Run-level metrics over a batch of processed invoices: outcomes, dollars, and model cost."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .config import Settings
from .ledger import content_hash
from .llm import Usage
from .models import Outcome
from .state import PipelineState


@dataclass
class Metrics:
    total: int
    paid: int
    rejected: int
    escalated: int
    dollars_paid: float
    dollars_held: float
    model_calls: int
    input_tokens: int
    output_tokens: int
    latency_ms: float
    cost: float | None

    def _rate(self, count: int) -> float:
        return count / self.total if self.total else 0.0

    @property
    def straight_through_rate(self) -> float:
        return self._rate(self.paid)

    @property
    def escalation_rate(self) -> float:
        return self._rate(self.escalated)

    @property
    def rejection_rate(self) -> float:
        return self._rate(self.rejected)


def estimated_cost(usage: Iterable[Usage], settings: Settings) -> float | None:
    """Dollar estimate from token counts. Returns None unless both prices are configured,
    since token prices change and should not be hard-coded."""
    if settings.price_in_per_mtok is None or settings.price_out_per_mtok is None:
        return None
    return sum(
        u.input_tokens * settings.price_in_per_mtok + u.output_tokens * settings.price_out_per_mtok for u in usage
    ) / 1_000_000


def summarize(states: list[PipelineState], usage: list[Usage], settings: Settings) -> Metrics:
    # Count dollars per unique invoice: an exact duplicate re-submission is the same money,
    # so it is not added again. Outcome counts still reflect every file processed.
    seen: set[str] = set()
    dollars_paid = dollars_held = 0.0
    for state in states:
        if not state.invoice or state.outcome is None:
            continue
        key = content_hash(state.invoice)
        if key in seen:
            continue
        seen.add(key)
        amount = state.invoice.total or 0
        if state.outcome is Outcome.PAID:
            dollars_paid += amount
        elif state.outcome in (Outcome.ESCALATED, Outcome.REJECTED):
            dollars_held += amount

    return Metrics(
        total=len(states),
        paid=sum(1 for s in states if s.outcome is Outcome.PAID),
        rejected=sum(1 for s in states if s.outcome is Outcome.REJECTED),
        escalated=sum(1 for s in states if s.outcome is Outcome.ESCALATED),
        dollars_paid=round(dollars_paid, 2),
        dollars_held=round(dollars_held, 2),
        model_calls=len(usage),
        input_tokens=sum(u.input_tokens for u in usage),
        output_tokens=sum(u.output_tokens for u in usage),
        latency_ms=round(sum(u.latency_ms for u in usage), 1),
        cost=estimated_cost(usage, settings),
    )


def format_metrics(m: Metrics) -> str:
    lines = [
        "Run metrics",
        f"  Invoices        {m.total}",
        f"  Paid            {m.paid}  ({m.straight_through_rate:.0%})",
        f"  Escalated       {m.escalated}  ({m.escalation_rate:.0%})",
        f"  Rejected        {m.rejected}  ({m.rejection_rate:.0%})",
        f"  Dollars paid    {m.dollars_paid:,.2f}",
        f"  Dollars held    {m.dollars_held:,.2f}",
        f"  Model calls     {m.model_calls}",
        f"  Tokens          {m.input_tokens:,} in / {m.output_tokens:,} out",
        f"  Model latency   {m.latency_ms:,.0f} ms",
    ]
    if m.cost is not None:
        lines.append(f"  Est. cost       ${m.cost:,.4f}")
    return "\n".join(lines)
