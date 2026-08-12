"""Turns — one question and answer inside a room.

A turn is not a message. One turn may involve several calls to Gemini: a tool
round-trip, a retry after a rate limit, a repair after malformed JSON. The student
sees one answer; the attempts underneath are recorded separately in
`turn_attempts`. That is why the brief asks to inspect a turn's "model attempts",
plural.

There is deliberately no `messages` table. A conversation is exactly the sequence
of turns in a room, each holding the student's message and the tutor's answer, so
a separate table would duplicate both columns and give nothing that a query over
`turns` cannot already answer. Intermediate tool exchanges are not conversation —
they live in `tool_calls` (S4) and in each attempt's request payload.

`status` is a string with a CHECK constraint rather than a Postgres enum. Adding a
value to an enum needs ALTER TYPE, which cannot run inside a transaction, and S3
will add failure states. A CHECK is one line to change and reverses cleanly.

Timing: `created_at` (from CreatedAtMixin) is the start and `completed_at` the
end. There is no stored duration — it is arithmetic on two columns of the same
row, so it is computed on read by `duration_ms` below. There is no `updated_at`
either: `completed_at` already records the last write, and more precisely.
"""

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    desc,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, CreatedAtMixin, UUIDPrimaryKey

if TYPE_CHECKING:
    from app.models.room import Room
    from app.models.turn_attempt import TurnAttempt


class TurnStatus(enum.StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class TurnFailureReason(enum.StrEnum):
    """Every reason a turn can fail, in one place.

    Listed together because this is what a client branches on, and a list spread
    across three modules is one nobody can read. The first three mirror
    `LLMError.error_type` exactly — a test asserts they stay in step, so the
    duplication cannot drift.
    """

    # The call itself did not produce usable text.
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    EMPTY_RESPONSE = "EMPTY_RESPONSE"
    RESPONSE_BLOCKED = "RESPONSE_BLOCKED"

    # We got text every time, and every version of it was rejected. The attempts
    # carry `validation_failures` saying which layer objected and why.
    INVALID_AFTER_REPAIR = "INVALID_AFTER_REPAIR"

    # The per-turn ceiling on model calls was reached. Unreachable with the
    # default budgets; see services/turn.py.
    ATTEMPT_LIMIT_EXCEEDED = "ATTEMPT_LIMIT_EXCEEDED"


TURN_STATUS_SQL_TUPLE = str(tuple(s.value for s in TurnStatus))


class Turn(UUIDPrimaryKey, CreatedAtMixin, Base):
    __tablename__ = "turns"

    room_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("rooms.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Position within the room, starting at 1. Gives a stable order that does not
    # depend on timestamps, which can tie when two turns land in the same
    # transaction.
    seq: Mapped[int] = mapped_column(Integer, nullable=False)

    student_message: Mapped[str] = mapped_column(Text, nullable=False)

    # NULL until the turn succeeds. A failed turn never has an answer — the whole
    # point is that nothing half-checked reaches the student.
    answer_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Did the question belong in this room? The tutor prompt has always told the
    # model to decline off-topic questions, but until S3 the verdict was buried in
    # prose and nothing could act on it. Stored, it has three readers: the context
    # builder skips off-topic turns so a detour does not pollute the next prompt,
    # study history excludes them so a stray question is not counted as studying
    # the subject, and the API returns it. NULL only while the turn is unfinished.
    in_scope: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # Short topic tags for what this turn taught, e.g. ["inverse operations"].
    #
    # A JSONB list rather than a turn_concepts join table, deliberately. Concepts
    # here are per-turn labels, never entities in their own right: nothing renames
    # them, merges them, or hangs data off them. The only questions asked of them —
    # "what did this student study between two dates", "which turns touched X" —
    # are answered by a GIN index on this column without a join. A table would
    # become the right answer the moment a concept needs an identity of its own.
    concepts: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=TurnStatus.RUNNING.value,
    )

    # Typed reason, e.g. PROVIDER_UNAVAILABLE, TURN_DEADLINE_EXCEEDED. Set only
    # when status is failed, so a client never has to parse a message.
    failure_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # NULL while the turn is still running.
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    room: Mapped["Room"] = relationship()
    attempts: Mapped[list["TurnAttempt"]] = relationship(
        back_populates="turn",
        cascade="all, delete-orphan",
        order_by="TurnAttempt.attempt_no",
    )

    @property
    def duration_ms(self) -> int | None:
        """How long the student waited. Computed, never stored.

        Covers everything: room lookup, prompt rendering, database writes and
        every attempt. So it always exceeds the sum of the attempts' own
        durations, and the difference is our own overhead — which becomes the
        cost of the repair loop in S3.
        """
        if self.completed_at is None:
            return None
        return int((self.completed_at - self.created_at).total_seconds() * 1000)

    __table_args__ = (
        # Turn numbers are unique within a room, so the sequence cannot fork if
        # two requests race.
        UniqueConstraint("room_id", "seq", name="uq_turns_room_id_seq"),
        CheckConstraint(f"status IN {TURN_STATUS_SQL_TUPLE}", name="status_is_valid"),
        CheckConstraint("seq > 0", name="seq_is_positive"),
        # An answer only exists on success, and a failure always says why. A
        # succeeded turn also knows whether it was in scope — that verdict is part
        # of a complete answer, not an optional extra.
        CheckConstraint(
            "(status = 'succeeded' AND answer_text IS NOT NULL AND in_scope IS NOT NULL)"
            " OR (status = 'failed' AND failure_reason IS NOT NULL)"
            " OR status = 'running'",
            name="terminal_states_are_complete",
        ),
        # `reliability.checks.tidy` already clears the tags on an out-of-scope
        # turn, in code and for free, so nothing the pipeline writes can break
        # this. That is exactly what makes it a safety net rather than a duplicate
        # rule: it holds for rows that arrive from a script or a future code path
        # that forgets. Study history depends on it — an off-topic turn must not
        # contribute study time.
        CheckConstraint(
            "in_scope IS NOT FALSE OR concepts IS NULL OR concepts = '[]'::jsonb",
            name="off_topic_turns_teach_nothing",
        ),
        # The room timeline: newest turn first.
        Index("ix_turns_room_id_seq", "room_id", desc("seq")),
        # Answers "which turns touched this concept" without reading every row.
        # GIN is the index type for containment queries on JSONB (`concepts ? 'x'`);
        # a B-tree cannot answer them, because the value is a list, not a scalar.
        Index(
            "ix_turns_concepts",
            "concepts",
            postgresql_using="gin",
            postgresql_where=text("concepts IS NOT NULL"),
        ),
    )
