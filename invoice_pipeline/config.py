"""Runtime settings. Every threshold, tolerance, and model name lives here."""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass, replace
from pathlib import Path

from dotenv import load_dotenv

# Urgency, manipulation, and prompt-injection language that should never be auto-approved.
PRESSURE_PHRASES = (
    "urgent", "immediate", "avoid penalties", "wire transfer", "asap",
    "as soon as possible", "act now", "final notice", "ignore previous",
    "ignore all previous", "disregard", "approve this invoice", "do not verify",
)

ROOT = Path(__file__).resolve().parent.parent

load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Tolerance:
    """Allowed difference before a flag is raised: the larger of a fraction and an absolute amount.

    Overcharges are held to a tight band and undercharges to a looser one, since paying
    too much is a loss and being billed too little is not.
    """

    over_pct: float
    over_abs: float
    under_pct: float
    under_abs: float

    def over_band(self, base: float) -> float:
        return max(abs(base) * self.over_pct, self.over_abs)

    def under_band(self, base: float) -> float:
        return max(abs(base) * self.under_pct, self.under_abs)


@dataclass(frozen=True)
class Settings:
    db_path: Path
    runs_dir: Path
    scrutiny_threshold: float = 10_000.0
    cfo_threshold: float = 50_000.0
    home_currency: str = "USD"
    settleable_currencies: tuple[str, ...] = ("USD",)
    as_of_date: dt.date | None = None
    split_billing_window_days: int = 30
    pressure_phrases: tuple[str, ...] = PRESSURE_PHRASES
    extraction_max_retries: int = 2
    critic_max_rounds: int = 2
    arithmetic_tolerance: Tolerance = Tolerance(over_pct=0.001, over_abs=1.00, under_pct=0.05, under_abs=1.00)
    price_tolerance: Tolerance = Tolerance(over_pct=0.001, over_abs=0.01, under_pct=0.10, under_abs=0.01)
    xai_api_key: str | None = None
    xai_base_url: str = "https://api.x.ai/v1"
    extraction_model: str = "grok-4.3"
    approval_model: str = "grok-4.6"
    llm_timeout_seconds: float = 60.0
    llm_max_retries: int = 3
    # Token prices per million tokens. Set to estimate cost; left unset, cost is not reported.
    price_in_per_mtok: float | None = None
    price_out_per_mtok: float | None = None


def _float_env(name: str) -> float | None:
    value = os.getenv(name)
    try:
        return float(value) if value else None
    except ValueError:
        return None


def load_settings(**overrides) -> Settings:
    """Build settings from the environment, with explicit overrides taking precedence."""
    settings = Settings(
        db_path=Path(os.getenv("INVOICE_DB_PATH") or ROOT / "inventory.db"),
        runs_dir=ROOT / "runs",
        xai_api_key=os.getenv("XAI_API_KEY") or None,
        xai_base_url=os.getenv("XAI_BASE_URL") or Settings.xai_base_url,
        extraction_model=os.getenv("EXTRACTION_MODEL") or Settings.extraction_model,
        approval_model=os.getenv("APPROVAL_MODEL") or Settings.approval_model,
        price_in_per_mtok=_float_env("INVOICE_PRICE_IN_PER_MTOK"),
        price_out_per_mtok=_float_env("INVOICE_PRICE_OUT_PER_MTOK"),
    )
    return replace(settings, **overrides)
