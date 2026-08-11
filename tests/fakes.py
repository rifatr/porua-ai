"""A stand-in for Gemini.

Records what it was asked, so tests can assert on the prompt that was built —
which is how we check that the education level and the room's earlier turns
actually reached the model, rather than just trusting the template.
"""

from app.llm.base import LLMError, LLMRequest, LLMResponse

DEFAULT_ANSWER = "Subtract 6 from both sides, then divide by 3. x = 3."


class FakeLLM:
    """Returns queued responses, or raises a queued error.

    Every request is kept in `.calls`, so a test can inspect exactly what was
    sent without reaching into the service.
    """

    def __init__(
        self,
        *,
        answers: list[str] | None = None,
        error: LLMError | None = None,
        prompt_tokens: int = 100,
        output_tokens: int = 40,
        thought_tokens: int = 250,
    ) -> None:
        self._answers = list(answers) if answers else None
        self._error = error
        self._tokens = (prompt_tokens, output_tokens, thought_tokens)
        self.calls: list[LLMRequest] = []

    @property
    def last_prompt(self) -> str:
        return self.calls[-1].prompt

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)

        if self._error is not None:
            raise self._error

        text = self._answers.pop(0) if self._answers else DEFAULT_ANSWER

        prompt_tokens, output_tokens, thought_tokens = self._tokens
        return LLMResponse(
            text=text,
            finish_reason="STOP",
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
            thought_tokens=thought_tokens,
            raw=text,
        )
