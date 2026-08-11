"""Turn attempts — one row per call to Gemini, including the calls that failed.

This is the most important table in the project. The brief asks to "inspect a turn
in enough detail to understand its prompt, model attempts, tool activity,
failures, token usage, and final saved result", which is really a description of
what to store. Because attempts are real rows from the first migration that needs
them, `GET /turns/{id}` is a query rather than a retrofit — log files could not
answer it afterwards.

Failed attempts matter more than successful ones. A turn that took three tries
tells you something a turn that worked first time does not, and none of it is
recoverable later if it was never written down.

No created_at/updated_at mixin here, and the reason is not that the row is never
edited — it is written before the call and updated after. It is that Postgres'
now() returns the *transaction* start time, so several attempts made while
answering one turn would all carry an identical created_at. That would defeat the
one question this table exists to answer: which attempt was slow. Each attempt
therefore stamps its own start from the application clock.
"""

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, UUIDPrimaryKey

if TYPE_CHECKING:
    from app.models.turn import Turn


class AttemptPurpose(enum.StrEnum):
    """Why this call was made. S3 adds REPAIR; S6 adds SKILL."""

    TUTOR = "tutor"
    REPAIR = "repair"
    SKILL = "skill"
    SUMMARIZE = "summarize"


ATTEMPT_PURPOSE_SQL_TUPLE = str(tuple(p.value for p in AttemptPurpose))


class TurnAttempt(UUIDPrimaryKey, Base):
    """No timestamp mixins — see the module docstring for why."""

    __tablename__ = "turn_attempts"

    turn_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("turns.id", ondelete="CASCADE"),
        nullable=False,
    )

    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    purpose: Mapped[str] = mapped_column(String(16), nullable=False)

    # Which prompt produced this, exactly. The checksum is what makes the link
    # reliable: a prompt file edited in place would otherwise silently change the
    # meaning of every attempt that already referenced its version.
    prompt_name: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt_sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    # The fully rendered text actually sent, not the template.
    rendered_prompt: Mapped[str] = mapped_column(Text, nullable=False)

    # model, temperature, max tokens, and later the tool declarations.
    request_params: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    raw_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    finish_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)

    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    thought_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Set when the call itself failed: a timeout, a 429, a refusal.
    error_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Filled by S3: which content checks the response failed, so the inspection
    # endpoint can show why an answer was rejected and repaired.
    validation_failures: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    turn: Mapped["Turn"] = relationship(back_populates="attempts")

    @property
    def duration_ms(self) -> int | None:
        """How long this one call took. Computed, never stored.

        Comparing this with the turn's own duration separates time spent waiting
        on the model from time spent in our code.
        """
        if self.finished_at is None:
            return None
        return int((self.finished_at - self.started_at).total_seconds() * 1000)

    __table_args__ = (
        UniqueConstraint("turn_id", "attempt_no", name="uq_turn_attempts_turn_id_attempt_no"),
        CheckConstraint(
            f"purpose IN {ATTEMPT_PURPOSE_SQL_TUPLE}", name="purpose_is_valid"
        ),
        CheckConstraint("attempt_no > 0", name="attempt_no_is_positive"),
    )
