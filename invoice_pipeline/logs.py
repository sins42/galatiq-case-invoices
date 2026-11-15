"""Structured logging: one JSON object per line on stderr."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

LOGGER_NAME = "invoice_pipeline"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="seconds"),
            "level": record.levelname.lower(),
            "event": record.getMessage(),
        }
        entry.update(getattr(record, "fields", {}))
        return json.dumps(entry, default=str)


def configure(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger(LOGGER_NAME)
    logger.handlers[:] = [handler]
    logger.setLevel(level.upper())
    logger.propagate = False


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def log_event(event: str, level: int = logging.INFO, **fields: Any) -> None:
    get_logger().log(level, event, extra={"fields": fields})
