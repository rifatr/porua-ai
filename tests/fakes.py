"""A stand-in for Gemini.

Records what it was asked, so tests can assert on the prompt that was built —
which is how we check that the education level and the room's earlier turns
actually reached the model, rather than just trusting the template.

`answers` is a queue, and that is the whole point in S3: a test can line up "junk,
then junk, then something valid" and assert the repair loop got there. Whatever
the model would really have done, these are the sequences we need to be correct
for, and the real model will not perform them on request.
"""

import json

from app.llm.base import LLMError, LLMRequest, LLMResponse, ToolInvocation

DEFAULT_ANSWER = "Subtract 6 from both sides, then divide by 3. x = 3."
DEFAULT_CONCEPTS = ["inverse operations", "linear equations"]


def tutor_json(
    answer: str = DEFAULT_ANSWER,
    *,
    in_scope: bool = True,
    concepts: list[str] | None = None,
) -> str:
    """A well-formed tutor response, as the model would send it.

    Tests that are not about reliability use this so they keep testing the thing
    they are named after rather than accidentally testing the parser.
    """
    return json.dumps(
        {
            "answer": answer,
            "in_scope": in_scope,
            "concepts": DEFAULT_CONCEPTS if concepts is None else concepts,
        }
    )


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
        errors: list[LLMError | None] | None = None,
        tool_calls: list[list[tuple[str, dict]] | None] | None = None,
        prompt_tokens: int = 100,
        output_tokens: int = 40,
        thought_tokens: int = 250,
    ) -> None:
        self._answers = list(answers) if answers else None
        # `error` fails every call; `errors` fails only the calls it lists, which
        # is what a transient rate limit looks like — fail, fail, then succeed.
        self._error = error
        self._errors = list(errors) if errors else None
        # One entry per call: a list of (tool_name, arguments) to request, or None
        # to answer instead. This is how a test drives the agent loop — asking the
        # real model to call a tool twice, or to name one that does not exist, is
        # not something it will do on request.
        self._tool_calls = list(tool_calls) if tool_calls else None
        self._tokens = (prompt_tokens, output_tokens, thought_tokens)
        self.calls: list[LLMRequest] = []

    @property
    def last_prompt(self) -> str:
        return self.calls[-1].prompt

    @property
    def prompts(self) -> list[str]:
        return [call.prompt for call in self.calls]

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)

        if self._error is not None:
            raise self._error

        if self._errors:
            queued = self._errors.pop(0)
            if queued is not None:
                raise queued

        prompt_tokens, output_tokens, thought_tokens = self._tokens

        if self._tool_calls:
            requested = self._tool_calls.pop(0)
            # Only when tools were actually offered. The real model cannot call a
            # tool that was not declared to it, so a fake that ignores this would
            # let the runaway-loop tests pass against a loop that never stops
            # offering tools — testing the opposite of what they claim.
            if requested and not request.tools:
                requested = None
            if requested:
                # A response that asks for tools carries no answer text, exactly
                # as the real client reports it.
                return LLMResponse(
                    text="",
                    finish_reason="STOP",
                    prompt_tokens=prompt_tokens,
                    output_tokens=output_tokens,
                    thought_tokens=thought_tokens,
                    raw="",
                    tool_calls=tuple(
                        ToolInvocation(name=name, arguments=arguments)
                        for name, arguments in requested
                    ),
                )

        # The queue runs out rather than repeating: a test that expected three
        # responses and got a fourth call has found a bug in the loop, and should
        # see a valid answer rather than an index error.
        text = self._answers.pop(0) if self._answers else tutor_json()

        return LLMResponse(
            text=text,
            finish_reason="STOP",
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
            thought_tokens=thought_tokens,
            raw=text,
        )
