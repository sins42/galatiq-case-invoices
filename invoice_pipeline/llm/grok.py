"""xAI Grok through its OpenAI-compatible API."""

from __future__ import annotations

import json
import time
from typing import Any

import openai

from ..config import Settings
from .base import LLMError, LLMRequest, LLMResponse, Usage, run_tool

_MAX_TOOL_STEPS = 6


class GrokProvider:
    name = "grok"
    is_mock = False

    def __init__(self, settings: Settings):
        if not settings.xai_api_key:
            raise LLMError("XAI_API_KEY is not set")
        # The client retries rate limits, timeouts, and server errors with backoff.
        self._client = openai.OpenAI(
            api_key=settings.xai_api_key,
            base_url=settings.xai_base_url,
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )
        self._models = {"extraction": settings.extraction_model, "approval": settings.approval_model}
        self.usage: list[Usage] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        started = time.perf_counter()
        model = self._models[request.role]
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": request.system},
            {"role": "user", "content": request.user},
        ]
        kwargs: dict[str, Any] = {}
        if request.tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
                }
                for t in request.tools
            ]
        calls: list[dict[str, Any]] = []
        input_tokens = output_tokens = 0

        for _ in range(_MAX_TOOL_STEPS):
            try:
                response = self._client.chat.completions.create(
                    model=model, messages=messages, temperature=0, **kwargs
                )
            except openai.OpenAIError as exc:
                raise LLMError(f"Grok request failed: {exc}") from exc

            usage = getattr(response, "usage", None)
            if usage:
                input_tokens += getattr(usage, "prompt_tokens", 0) or 0
                output_tokens += getattr(usage, "completion_tokens", 0) or 0

            message = response.choices[0].message
            if not message.tool_calls:
                self.usage.append(Usage(request.role, model, input_tokens, output_tokens, (time.perf_counter() - started) * 1000))
                return LLMResponse(text=message.content or "", tool_calls=calls)

            messages.append(
                {
                    "role": "assistant",
                    "content": message.content or "",
                    "tool_calls": [tc.model_dump() for tc in message.tool_calls],
                }
            )
            for tc in message.tool_calls:
                try:
                    arguments = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                result = run_tool(request.tools, tc.function.name, arguments)
                calls.append({"tool": tc.function.name, "arguments": arguments, "result": result})
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result, default=str)})

        raise LLMError("model kept calling tools without answering")
