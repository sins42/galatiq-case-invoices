"""Deterministic offline provider. Used when no API key is set, and in tests.

It answers from the structured request context with fixed rules, so runs are reproducible.
Tests can queue scripted responses per task to exercise the self-correction loops.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from .base import LLMRequest, LLMResponse, run_tool
from .text_parser import parse_text_invoice

Scripted = str | Callable[[LLMRequest], str]


class MockProvider:
    name = "mock"
    is_mock = True

    def __init__(self, script: dict[str, list[Scripted]] | None = None):
        self._script = {task: list(items) for task, items in (script or {}).items()}

    def complete(self, request: LLMRequest) -> LLMResponse:
        queued = self._script.get(request.task)
        if queued:
            item = queued.pop(0)
            return LLMResponse(text=item(request) if callable(item) else item)
        handler = {"extract": self._extract, "approve": self._approve, "critique": self._critique}[request.task]
        return handler(request)

    @staticmethod
    def _extract(request: LLMRequest) -> LLMResponse:
        return LLMResponse(text=json.dumps(parse_text_invoice(request.context["source_text"])))

    @staticmethod
    def _approve(request: LLMRequest) -> LLMResponse:
        # Look up the vendor's history the way the real agent would, then apply a fixed rule:
        # approve only when validation raised nothing; otherwise defer to a person.
        vendor = request.context["invoice"].get("vendor") or ""
        history = run_tool(request.tools, "get_vendor_history", {"vendor": vendor}) if request.tools else None
        calls = [{"tool": "get_vendor_history", "arguments": {"vendor": vendor}, "result": history}] if history is not None else []

        warnings = [f["code"] for f in request.context["findings"]]
        if warnings:
            decision = {"decision": "escalate", "rationale": f"Unresolved warnings need a person: {', '.join(warnings)}."}
        else:
            decision = {
                "decision": "approve",
                "rationale": "No validation findings. Amount is within the agent's approval limit.",
            }
        return LLMResponse(text=json.dumps(decision), tool_calls=calls)

    @staticmethod
    def _critique(request: LLMRequest) -> LLMResponse:
        warnings = [f["code"] for f in request.context["findings"]]
        if warnings:
            verdict: dict[str, Any] = {
                "verdict": "object",
                "cited": warnings[0],
                "objection": f"The approval does not resolve {warnings[0]}.",
            }
        else:
            verdict = {"verdict": "accept"}
        return LLMResponse(text=json.dumps(verdict))
