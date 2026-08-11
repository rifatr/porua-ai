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
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    desc,
)
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
        # An answer only exists on success, and a failure always says why.
        CheckConstraint(
            "(status = 'succeeded' AND answer_text IS NOT NULL)"
            " OR (status = 'failed' AND failure_reason IS NOT NULL)"
            " OR status = 'running'",
            name="terminal_states_are_complete",
        ),
        # The room timeline: newest turn first.
        Index("ix_turns_room_id_seq", "room_id", desc("seq")),
    )
