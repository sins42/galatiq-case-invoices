"""Per-invoice audit records written as JSON files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import AuditEvent


def write_record(runs_dir: Path, run_id: str, source_file: str, record: dict[str, Any]) -> Path:
    folder = runs_dir / run_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{Path(source_file).stem}.json"
    path.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    return path


def event(stage: str, outcome: str, reason: str, **detail: Any) -> AuditEvent:
    return AuditEvent.now(stage, outcome, reason, **detail)
