"""The contract between the application and whatever is generating text.

Everything above this layer talks to `LLMClient`, never to Google's SDK. That is
what lets the entire test suite run with no API key, and what would make swapping
the provider a single-file change.
"""

from dataclasses import dataclass, field
from typing import Protocol


class LLMError(Exception):
    """A call that did not produce a usable response.

    `error_type` is a stable, machine-readable string. It is stored on the attempt
    row and, when a turn fails, surfaces as the turn's failure_reason — so a
    client can branch on it without parsing prose.
    """

    def __init__(self, error_type: str, detail: str) -> None:
        self.error_type = error_type
        self.detail = detail
        super().__init__(f"{error_type}: {detail}")


class ProviderUnavailable(LLMError):
    """The provider could not be reached, or returned an error we did not cause."""

    def __init__(self, detail: str) -> None:
        super().__init__("PROVIDER_UNAVAILABLE", detail)


class EmptyResponse(LLMError):
    """The call succeeded but there is no usable text.

    The common cause with gemini-2.5-flash is worth knowing: it is a thinking
    model, and thinking tokens count against max_output_tokens. If that budget is
    too small the model can spend all of it reasoning and return an empty answer
    with finish_reason MAX_TOKENS. It looks like a model failure and is really a
    configuration mistake.
    """

    def __init__(self, detail: str) -> None:
        super().__init__("EMPTY_RESPONSE", detail)


class ResponseBlocked(LLMError):
    """The provider refused to answer, usually on safety grounds."""

    def __init__(self, detail: str) -> None:
        super().__init__("RESPONSE_BLOCKED", detail)


@dataclass(frozen=True)
class LLMResponse:
    text: str
    finish_reason: str
    prompt_tokens: int = 0
    output_tokens: int = 0
    # Billed, but never part of `text`. Tracked separately so cost per turn is
    # honest and so we can tell "the model rambled" from "the model thought hard".
    thought_tokens: int = 0
    raw: str = ""

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.output_tokens + self.thought_tokens


@dataclass(frozen=True)
class LLMRequest:
    prompt: str
    model: str
    temperature: float
    max_output_tokens: int = 4096
    extra: dict = field(default_factory=dict)

    def as_params(self) -> dict:
        """What gets stored in turn_attempts.request_params.

        The prompt is not included — it has its own column, and duplicating it in
        JSON would double the storage on the largest field in the table.
        """
        return {
            "model": self.model,
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
            **self.extra,
        }


class LLMClient(Protocol):
    async def generate(self, request: LLMRequest) -> LLMResponse: ...
