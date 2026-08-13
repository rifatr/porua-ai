"""The contract between the application and whatever is generating text.

Everything above this layer talks to `LLMClient`, never to Google's SDK. That is
what lets the entire test suite run with no API key, and what would make swapping
the provider a single-file change.
"""

from dataclasses import dataclass, field
from typing import Protocol

from pydantic import BaseModel


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
class ToolSpec:
    """What the model is told a tool can do.

    `parameters` is a JSON Schema object, produced from the tool's Pydantic
    argument model. Describing the tool in a standard format rather than a
    Gemini-specific one is what keeps this file provider-agnostic — the client
    translates it, nothing above the client knows the translation happened.
    """

    name: str
    description: str
    parameters: dict


@dataclass(frozen=True)
class ToolInvocation:
    """The model asking for a tool to run. Untrusted: these are model output.

    `arguments` is whatever the model produced. It is *not* validated here — the
    registry checks it against the tool's own Pydantic model before anything runs,
    which is what stops a malformed or hostile argument reaching a function.
    """

    name: str
    arguments: dict


@dataclass(frozen=True)
class ToolResult:
    """What we send back after running a tool."""

    name: str
    content: dict


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
    # Non-empty when the model asked for tools instead of answering. A response
    # carries one or the other, never usefully both.
    tool_calls: tuple[ToolInvocation, ...] = ()

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.output_tokens + self.thought_tokens


@dataclass(frozen=True)
class LLMRequest:
    prompt: str
    model: str
    temperature: float
    max_output_tokens: int = 4096
    # When set, the provider is asked to emit exactly this shape and nothing else.
    # It lives on the request rather than inside the Gemini client so the transport
    # stays ignorant of what a tutor answer is: any provider with a structured
    # output mode can honour it, and one without it can ignore the field — which is
    # what keeps `LLMClient` a Protocol rather than a Gemini interface.
    response_schema: type[BaseModel] | None = None

    # Tools the model may ask for on this call.
    #
    # `tools` and `response_schema` cannot both be set. That is not our rule, it is
    # the provider's — Gemini answers a request carrying both with
    # 400 "Function calling with a response mime type: 'application/json' is
    # unsupported". So a call either gathers information or produces a constrained
    # final answer, and `__post_init__` refuses the combination rather than letting
    # it fail at the API with a message nobody reads.
    tools: tuple[ToolSpec, ...] = ()

    # Tool calls already made on this turn, with their results, so the model can
    # see what it asked for and what came back. Grows by one pair per loop.
    tool_exchanges: tuple[tuple[ToolInvocation, ToolResult], ...] = ()

    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.tools and self.response_schema is not None:
            raise ValueError(
                "tools and response_schema cannot both be set: the provider "
                "rejects the combination. Attach tools while gathering, and a "
                "schema on the call that produces the final answer."
            )

    def as_params(self) -> dict:
        """What gets stored in turn_attempts.request_params.

        The prompt is not included — it has its own column, and duplicating it in
        JSON would double the storage on the largest field in the table.
        """
        return {
            "model": self.model,
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
            # The class name, not the expanded schema. request_params is for a
            # human reading the attempt back; the shape is recoverable from the
            # name plus the code at that commit, and inlining it would bury the
            # three fields that actually differ between attempts.
            "response_schema": (
                self.response_schema.__name__ if self.response_schema else None
            ),
            # Names only. Which tools were *offered* on this call is the thing
            # worth seeing when reading an attempt back — the full declarations
            # are the same on every call and would bury everything else.
            "tools": [spec.name for spec in self.tools],
            "tool_exchanges": len(self.tool_exchanges),
            **self.extra,
        }


class LLMClient(Protocol):
    async def generate(self, request: LLMRequest) -> LLMResponse: ...
