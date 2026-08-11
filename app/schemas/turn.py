"""Request and response shapes for turns.

Two response shapes on purpose:

- `TurnRead` is the conversation view — what the student asked, what the tutor
  said. Small, and safe to return in a list.
- `TurnDetail` is the inspection view behind `GET /turns/{id}`, and answers the
  brief's requirement to see "its prompt, model attempts, tool activity,
  failures, token usage, and final saved result".

They are separate because `TurnDetail` carries every rendered prompt and raw
response, which can be tens of kilobytes. Returning that from a list endpoint
would make the room timeline enormous for no reason.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class TurnCreate(BaseModel):
    message: str = Field(
        min_length=1,
        max_length=4000,
        description="The student's question.",
        examples=["How do I solve 3x + 6 = 15?"],
    )


class TokenUsage(BaseModel):
    prompt: int
    output: int
    thought: int = Field(
        description=(
            "Tokens gemini-2.5-flash spent reasoning before answering. Billed, "
            "but never part of the answer text."
        )
    )
    total: int


class TurnRead(BaseModel):
    """The conversation view."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    seq: int
    student_message: str
    answer_text: str | None
    status: str
    failure_reason: str | None = Field(
        default=None,
        description="Typed reason, set only when status is 'failed'.",
    )
    created_at: datetime

    # No timing here on purpose. This is the conversation view — a chat
    # timeline — and how many milliseconds a reply took is noise in it.
    # Timing belongs to the inspection view; see TurnDetail.


class AttemptRead(BaseModel):
    """One call to the model, successful or not."""

    model_config = ConfigDict(from_attributes=True)

    attempt_no: int
    purpose: str
    prompt_name: str
    prompt_version: int
    prompt_sha256: str = Field(
        description=(
            "Checksum of the exact prompt file used. Lets any stored answer be "
            "traced back to the text that produced it."
        )
    )
    rendered_prompt: str = Field(description="The full text actually sent.")
    request_params: dict
    raw_response: str | None
    finish_reason: str | None
    prompt_tokens: int | None
    output_tokens: int | None
    thought_tokens: int | None
    error_type: str | None
    error_detail: str | None
    validation_failures: list | None = Field(
        default=None,
        description="Which content checks this response failed. Populated from S3.",
    )
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None = Field(
        default=None,
        description=(
            "How long this single call took. Compare with the turn's duration to "
            "separate model time from our own."
        ),
    )


class TurnDetail(TurnRead):
    """The inspection view: everything that happened while producing this turn.

    Timing appears here and not on TurnRead because explaining what happened is
    this endpoint's entire job. Both the raw fact (`completed_at`) and the derived
    convenience (`duration_ms`) are given, so a client that wants to measure
    something else is not forced through ours.
    """

    room_id: UUID
    completed_at: datetime | None = Field(
        default=None,
        description="When the turn finished. Null while it is still running.",
    )
    duration_ms: int | None = Field(
        default=None,
        description=(
            "How long the student waited, in milliseconds — created_at to "
            "completed_at. Computed on read, not stored. Always larger than the "
            "sum of the attempts' own durations; the difference is our own work "
            "outside the model calls."
        ),
    )
    tokens: TokenUsage
    attempts: list[AttemptRead]

    @classmethod
    def from_model(cls, turn) -> "TurnDetail":
        # Summed from the attempts rather than stored on the turn. The attempts
        # are already loaded here, so the totals are free — and there is no second
        # copy that could drift out of step with them.
        # A failed attempt reports no tokens, hence the `or 0`.
        prompt = sum(a.prompt_tokens or 0 for a in turn.attempts)
        output = sum(a.output_tokens or 0 for a in turn.attempts)
        thought = sum(a.thought_tokens or 0 for a in turn.attempts)

        return cls(
            id=turn.id,
            room_id=turn.room_id,
            seq=turn.seq,
            student_message=turn.student_message,
            answer_text=turn.answer_text,
            status=turn.status,
            failure_reason=turn.failure_reason,
            created_at=turn.created_at,
            completed_at=turn.completed_at,
            duration_ms=turn.duration_ms,
            tokens=TokenUsage(
                prompt=prompt,
                output=output,
                thought=thought,
                total=prompt + output + thought,
            ),
            attempts=[AttemptRead.model_validate(a) for a in turn.attempts],
        )
