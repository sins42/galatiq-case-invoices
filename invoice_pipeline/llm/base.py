"""The provider interface every model backend implements."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

Task = Literal["extract", "approve", "critique"]
Role = Literal["extraction", "approval"]


class LLMError(Exception):
    """The model call failed after the client's own retries."""


@dataclass(frozen=True)
class Tool:
    """A read-only function the model may call."""

    name: str
    description: str
    parameters: dict[str, Any]
    run: Callable[..., Any]


@dataclass
class LLMRequest:
    task: Task
    role: Role
    system: str
    user: str
    # Structured inputs behind the prompt. The offline provider reads these instead of the text.
    context: dict[str, Any] = field(default_factory=dict)
    tools: list[Tool] = field(default_factory=list)


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Usage:
    """One model call's token counts and latency, so runs can be measured and costed."""

    role: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0


class Provider(Protocol):
    name: str
    is_mock: bool
    usage: list[Usage]

    def complete(self, request: LLMRequest) -> LLMResponse: ...


def run_tool(tools: list[Tool], name: str, arguments: dict[str, Any]) -> Any:
    """Execute a tool call, returning an error payload instead of raising."""
    tool = next((t for t in tools if t.name == name), None)
    if tool is None:
        return {"error": f"unknown tool '{name}'"}
    try:
        return tool.run(**arguments)
    except Exception as exc:  # a bad tool call should not crash the agent
        return {"error": str(exc)}


def parse_json_object(text: str) -> dict[str, Any]:
    """Extract the first JSON object from model output, tolerating code fences and prose."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("no JSON object in model output")
    return json.loads(match.group())
