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
    ToolInvocation,
)

logger = logging.getLogger(__name__)


class GeminiClient:
    def __init__(self, api_key: str, timeout_seconds: int = 30) -> None:
        if not api_key or api_key == "replace-me":
            raise ValueError("GEMINI_API_KEY is not set.")
        # Milliseconds — the SDK's unit, and worth stating because passing
        # seconds here would set a 30ms timeout and fail every call in a way that
        # looks like a network fault rather than a configuration one.
        self._client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=timeout_seconds * 1000),
        )

    async def generate(self, request: LLMRequest) -> LLMResponse:
        config = types.GenerateContentConfig(
            temperature=request.temperature,
            max_output_tokens=request.max_output_tokens,
        )

        if request.response_schema is not None:
            # Constrained decoding. The provider emits only tokens that fit the
            # schema, which makes one specific failure impossible rather than
            # merely discouraged: answering in prose first and then restating the
            # whole thing as JSON. That draft costs half the output budget and
            # leaves the real object truncated, and no wording in the prompt
            # reliably prevents it — under a schema it has nowhere to go.
            #
            # Both fields are set together on purpose — a schema without the mime
            # type is silently ignored, which would look like this working.
            config.response_mime_type = "application/json"
            config.response_schema = request.response_schema

        if request.tools:
            # `parameters_json_schema` takes the Pydantic schema as-is, so the
            # declaration the model sees is generated from the same class the
            # arguments are later validated against. They cannot drift apart.
            config.tools = [
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name=spec.name,
                            description=spec.description,
                            parameters_json_schema=spec.parameters,
                        )
                        for spec in request.tools
                    ]
                )
            ]

        try:
            response = await self._client.aio.models.generate_content(
                model=request.model,
                contents=self._contents(request),
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
    def _contents(request: LLMRequest) -> list[types.Content]:
        """The conversation so far: the prompt, then each tool call and its result.

        Function calling is multi-turn by nature. The model has to see that it
        asked for `evaluate_expression`, and what came back, or it will simply ask
        again. So every exchange is replayed on the next call, in the two-message
        shape the API expects: a `model` turn holding the call, a `user` turn
        holding the response.
        """
        contents = [types.Content(role="user", parts=[types.Part(text=request.prompt)])]

        for invocation, result in request.tool_exchanges:
            contents.append(
                types.Content(
                    role="model",
                    parts=[
                        types.Part(
                            function_call=types.FunctionCall(
                                name=invocation.name, args=invocation.arguments
                            )
                        )
                    ],
                )
            )
            contents.append(
                types.Content(
                    role="user",
                    parts=[
                        types.Part(
                            function_response=types.FunctionResponse(
                                name=result.name, response=result.content
                            )
                        )
                    ],
                )
            )

        return contents

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

        # Read the parts directly rather than `response.text`. A response holding
        # function calls has no usable `.text`, and touching it warns or returns
        # None — so the same accessor cannot serve both kinds of reply.
        content = getattr(candidate, "content", None)
        parts = list(getattr(content, "parts", None) or [])

        text = "".join(part.text for part in parts if getattr(part, "text", None))
        tool_calls = tuple(
            ToolInvocation(
                name=part.function_call.name,
                # dict() because the SDK hands back a mapping proxy, and this
                # value is stored as JSON and passed to Pydantic validation.
                arguments=dict(part.function_call.args or {}),
            )
            for part in parts
            if getattr(part, "function_call", None)
        )

        # A reply that asks for tools legitimately has no text. Only an empty
        # reply with nothing to run is a failure.
        if not text.strip() and not tool_calls:
            if finish_reason == "MAX_TOKENS":
                raise EmptyResponse(
                    "The model used its entire output budget on thinking and "
                    f"returned no answer (thought_tokens={thought_tokens}). "
                    "Raise max_output_tokens."
                )
            if finish_reason in {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST"}:
                raise ResponseBlocked(f"Provider refused to answer ({finish_reason}).")
            if finish_reason == "MALFORMED_FUNCTION_CALL":
                # The model tried to call a tool and produced something the
                # provider could not parse. Nondeterministic, so worth retrying.
                raise EmptyResponse(
                    "The model emitted a malformed function call and no answer."
                )
            raise EmptyResponse(f"Empty response with finish_reason={finish_reason}.")

        return LLMResponse(
            text=text,
            finish_reason=finish_reason,
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
            thought_tokens=thought_tokens,
            raw=text,
            tool_calls=tool_calls,
        )
