"""Choosing which LLM client the application uses."""

from functools import lru_cache

from app.config import get_settings
from app.llm.base import (
    EmptyResponse,
    LLMClient,
    LLMError,
    LLMRequest,
    LLMResponse,
    ProviderUnavailable,
    ResponseBlocked,
)

__all__ = [
    "EmptyResponse",
    "LLMClient",
    "LLMError",
    "LLMRequest",
    "LLMResponse",
    "ProviderUnavailable",
    "ResponseBlocked",
    "get_llm_client",
]


@lru_cache(maxsize=1)
def get_llm_client() -> LLMClient:
    """One client for the process, chosen by LLM_FIXTURE_MODE.

    Imports are inside the function so that `replay` mode never even loads the
    Google SDK — a test run cannot accidentally construct a real client.
    """
    settings = get_settings()
    mode = settings.llm_fixture_mode.lower()

    if mode == "replay":
        from app.llm.fixtures import ReplayClient

        return ReplayClient()

    from app.llm.gemini import GeminiClient

    real = GeminiClient(
        settings.gemini_api_key,
        timeout_seconds=settings.llm_request_timeout_seconds,
    )

    if mode == "record":
        from app.llm.fixtures import RecordingClient

        return RecordingClient(real)

    return real
