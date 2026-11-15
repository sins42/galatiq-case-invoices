"""Model providers behind one interface."""

from ..config import Settings
from .base import LLMError, LLMRequest, LLMResponse, Provider, Tool, Usage, parse_json_object
from .grok import GrokProvider
from .mock import MockProvider

__all__ = [
    "LLMError",
    "LLMRequest",
    "LLMResponse",
    "MockProvider",
    "Provider",
    "Tool",
    "Usage",
    "make_provider",
    "parse_json_object",
]


def make_provider(settings: Settings, offline: bool = False) -> Provider:
    """Grok when a key is set, otherwise the offline provider."""
    if offline or not settings.xai_api_key:
        return MockProvider()
    return GrokProvider(settings)
