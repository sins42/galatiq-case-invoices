"""Record real model responses to disk and replay them offline.

Each `complete` call is atomic from the pipeline's view, so one request maps to one recorded
response. Replay needs no API key or network.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .base import LLMError, LLMRequest, LLMResponse, Provider, Usage


def request_key(request: LLMRequest) -> str:
    """A stable key for a request. Tools are excluded: the recorded response already holds their results."""
    payload = {
        "task": request.task,
        "role": request.role,
        "system": request.system,
        "user": request.user,
        "context": request.context,
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _usage_to_dict(usage: Usage) -> dict:
    return {
        "role": usage.role,
        "model": usage.model,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "latency_ms": usage.latency_ms,
    }


class RecordingProvider:
    """Wraps a real provider and writes each request and response to a JSON file."""

    is_mock = False

    def __init__(self, inner: Provider, path: str | Path):
        self.inner = inner
        self.name = f"recording:{inner.name}"
        self.path = Path(path)
        self.usage = inner.usage  # share the list so run metrics see the real calls
        self._records: list[dict] = []
        if self.path.exists():
            self._records = json.loads(self.path.read_text()).get("records", [])

    def complete(self, request: LLMRequest) -> LLMResponse:
        before = len(self.inner.usage)
        response = self.inner.complete(request)
        made = self.inner.usage[before:]
        self._records.append(
            {
                "key": request_key(request),
                "text": response.text,
                "tool_calls": response.tool_calls,
                "usage": _usage_to_dict(made[-1]) if made else None,
            }
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"records": self._records}, indent=2))
        return response


class ReplayProvider:
    """Replays recorded responses by request key, in recorded order. No network, no API key."""

    name = "replay"
    is_mock = False

    def __init__(self, path: str | Path):
        try:
            data = json.loads(Path(path).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise LLMError(f"cannot read recording {path}: {exc}") from exc
        self._records = data.get("records", [])
        self._used: set[int] = set()
        self.usage: list[Usage] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        key = request_key(request)
        for index, record in enumerate(self._records):
            if index not in self._used and record["key"] == key:
                self._used.add(index)
                usage = record.get("usage")
                if usage:
                    self.usage.append(Usage(**usage))
                return LLMResponse(text=record["text"], tool_calls=record.get("tool_calls", []))
        raise LLMError(f"no recorded response for a {request.task}/{request.role} request; the recording is incomplete")
