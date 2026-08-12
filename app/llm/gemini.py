"""The real Gemini client.

Deliberately thin. It makes one call, normalises the response, and translates
provider errors into our own typed ones. Retries, validation and repair are S3's
job and wrap this rather than living inside it — so each layer can be tested on
its own.
"""

import logging

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from app.llm.base import (
    EmptyResponse,
    LLMRequest,
    LLMResponse,
    ProviderUnavailable,
    ResponseBlocked,
)

logger = logging.getLogger(__name__)


class GeminiClient:
    def __init__(self, api_key: str) -> None:
        if not api_key or api_key == "replace-me":
            raise ValueError("GEMINI_API_KEY is not set.")
        self._client = genai.Client(api_key=api_key)

    async def generate(self, request: LLMRequest) -> LLMResponse:
        config = types.GenerateContentConfig(
            temperature=request.temperature,
            max_output_tokens=request.max_output_tokens,
        )

        if request.response_schema is not None:
            # Constrained decoding. The provider will only emit tokens that fit the
            # schema, which makes one specific failure impossible rather than
            # merely discouraged: answering in prose first and then restating the
            # whole thing as JSON. That draft used to cost half the output budget
            # and leave the real object truncated; now it has nowhere to go.
            #
            # Both fields are set together on purpose — a schema without the mime
            # type is silently ignored, which would look like this working.
            config.response_mime_type = "application/json"
            config.response_schema = request.response_schema

        try:
            response = await self._client.aio.models.generate_content(
                model=request.model,
                contents=request.prompt,
                config=config,
            )
        except genai_errors.APIError as exc:
            # Covers rate limits, 5xx and malformed requests alike. S3 decides
            # which of these are worth retrying.
            raise ProviderUnavailable(f"{type(exc).__name__}: {exc}") from exc
        except Exception as exc:  # network failures, timeouts, DNS
            raise ProviderUnavailable(f"{type(exc).__name__}: {exc}") from exc

        return self._normalise(response)

    @staticmethod
    def _normalise(response: types.GenerateContentResponse) -> LLMResponse:
        usage = response.usage_metadata
        prompt_tokens = getattr(usage, "prompt_token_count", 0) or 0
        output_tokens = getattr(usage, "candidates_token_count", 0) or 0
        thought_tokens = getattr(usage, "thoughts_token_count", 0) or 0

        candidate = (response.candidates or [None])[0]
        finish_reason = str(getattr(candidate, "finish_reason", "") or "UNKNOWN")
        # "FinishReason.STOP" -> "STOP"
        finish_reason = finish_reason.rsplit(".", 1)[-1]

        text = response.text or ""

        if not text.strip():
            if finish_reason == "MAX_TOKENS":
                raise EmptyResponse(
                    "The model used its entire output budget on thinking and "
                    f"returned no answer (thought_tokens={thought_tokens}). "
                    "Raise max_output_tokens."
                )
            if finish_reason in {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST"}:
                raise ResponseBlocked(f"Provider refused to answer ({finish_reason}).")
            raise EmptyResponse(f"Empty response with finish_reason={finish_reason}.")

        return LLMResponse(
            text=text,
            finish_reason=finish_reason,
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
            thought_tokens=thought_tokens,
            raw=text,
        )
