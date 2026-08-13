"""Tool calls — one row every time the model asked for a tool.

Including the times it asked for a tool that does not exist, or passed arguments
that did not fit. Those rows matter more than the successful ones: they are the
evidence that the protections in `app/tools/` actually fired, and the brief asks
the inspection endpoint to show *"tool activity"* without saying "only the tool
activity that worked".

## Why this hangs off `turn_attempts` and not `turns`

A tool is requested by one specific model call. Hanging it off the attempt keeps
that link, so `GET /turns/{id}` reads as a story: this prompt produced this
request, which returned this, which led to the next prompt. Attached to the turn
instead, six tool calls across three attempts would arrive as a flat list with no
way to tell which call asked for what.

## Why arguments and result are both stored

`arguments` is model output and untrusted — stored exactly as received, before
validation, so a rejected call still shows what was actually asked for. `result`
is what we sent back. Together they are the whole exchange, and debugging a tool
loop without both is guesswork.
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
    from app.models.turn_attempt import TurnAttempt


class ToolStatus(enum.StrEnum):
    """How a tool call ended.

    Defined here, with the table that stores it, rather than in `app/tools/`.

    Dependencies run one way: `tools` may import `models`, because a tool needs the
    database — `query_study_history` queries it. `models` must not import `tools`.
    Keeping this enum here is what holds that line; defining it in `tools/base.py`
    would force this module to import back into `tools` and close a cycle.
    """

    OK = "ok"
    # The model asked for a tool that does not exist.
    UNKNOWN_TOOL = "unknown_tool"
    # The arguments did not fit the tool's model, so it never ran.
    INVALID_ARGUMENTS = "invalid_arguments"
    # The tool ran and refused the request — a bad expression, an impossible range.
    FAILED = "failed"
    # The tool ran too long and was cancelled.
    TIMED_OUT = "timed_out"


TOOL_STATUS_SQL_TUPLE = str(tuple(status.value for status in ToolStatus))


class ToolCall(UUIDPrimaryKey, Base):
    """No timestamp mixin, for the same reason as `turn_attempts`.

    Postgres' `now()` is the transaction start time, so several tool calls made
    while answering one turn would share an identical `created_at` and the order
    would be lost. Each call stamps its own start from the application clock.
    """

    __tablename__ = "tool_calls"

    attempt_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("turn_attempts.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Position within the turn, starting at 1. Counts across attempts, not within
    # one, because the budget it is checked against is per turn.
    call_no: Mapped[int] = mapped_column(Integer, nullable=False)

    # Exactly what the model asked for, even when no such tool exists. Stored as
    # text rather than a foreign key precisely because it may name nothing.
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)

    # Raw model output, before validation. A row with status invalid_arguments is
    # only useful if this shows what was actually sent.
    arguments: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    # What went back to the model. NULL when the call never ran.
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    status: Mapped[str] = mapped_column(String(20), nullable=False)

    # Why it failed, phrased for the model to read — it is sent back as the tool
    # result so the model can correct itself rather than repeat the mistake.
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    attempt: Mapped["TurnAttempt"] = relationship(back_populates="tool_calls")

    @property
    def duration_ms(self) -> int | None:
        """How long this one tool took. Computed, never stored.

        The number the per-tool timeout is set against, and the way to tell a slow
        tool from a slow model when a turn takes too long.
        """
        if self.finished_at is None:
            return None
        return int((self.finished_at - self.started_at).total_seconds() * 1000)

    __table_args__ = (
        UniqueConstraint("attempt_id", "call_no", name="uq_tool_calls_attempt_id_call_no"),
        CheckConstraint(f"status IN {TOOL_STATUS_SQL_TUPLE}", name="status_is_valid"),
        CheckConstraint("call_no > 0", name="call_no_is_positive"),
        # A successful call produced something; a failed one said why.
        CheckConstraint(
            "(status = 'ok' AND result IS NOT NULL)"
            " OR (status <> 'ok' AND error_detail IS NOT NULL)",
            name="tool_calls_explain_themselves",
        ),
        # No separate index for "every call for an attempt, in order". The UNIQUE
        # constraint above already builds a btree on (attempt_id, call_no), and
        # that is the index that query uses. A second one on the same columns
        # would cost a write on every insert and buy nothing.
    )
