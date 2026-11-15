"""File readers. Each format registers one reader; adding a format means adding one entry."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any


class IngestionError(Exception):
    """The file cannot be loaded. The invoice is escalated, not rejected."""


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # Malformed JSON is still readable as text, so extraction can fall back to the model.
        return path.read_text(encoding="utf-8")


READERS: dict[str, Callable[[Path], Any]] = {
    "txt": _read_text,
    "json": _read_json,
}

STRUCTURED_FORMATS = {"json"}


def load(path: Path) -> tuple[str, Any]:
    """Return (format, content) for a supported, non-empty file."""
    if not path.is_file():
        raise IngestionError(f"File not found: {path}")
    file_format = path.suffix.lower().lstrip(".")
    reader = READERS.get(file_format)
    if reader is None:
        raise IngestionError(f"Unsupported format '.{file_format}'")
    try:
        content = reader(path)
    except (OSError, UnicodeDecodeError) as exc:
        raise IngestionError(f"Could not read {path.name}: {exc}") from exc
    if content is None or (isinstance(content, str) and not content.strip()):
        raise IngestionError(f"{path.name} is empty")
    return file_format, content
