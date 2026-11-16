"""Model providers behind one interface."""

from ..config import Settings
from .base import LLMError, LLMRequest, LLMResponse, Provider, Tool, Usage, parse_json_object
from .grok import GrokProvider
from .mock import MockProvider
from .replay import RecordingProvider, ReplayProvider

__all__ = [
    "LLMError",
    "LLMRequest",
    "LLMResponse",
    "MockProvider",
    "Provider",
    "RecordingProvider",
    "ReplayProvider",
    "Tool",
    "Usage",
    "make_provider",
    "parse_json_object",
]


def make_provider(
    settings: Settings, offline: bool = False, record_path: str | None = None, replay_path: str | None = None
) -> Provider:
    """Pick a provider: replay a recording, record a live Grok run, or run Grok or the mock directly."""
    if record_path and replay_path:
        raise LLMError("use either --record or --replay, not both")
    if replay_path:
        return ReplayProvider(replay_path)
    if offline or not settings.xai_api_key:
        if record_path:
            raise LLMError("recording needs a live model; set XAI_API_KEY and do not use --offline")
        return MockProvider()
    provider = GrokProvider(settings)
    return RecordingProvider(provider, record_path) if record_path else provider
