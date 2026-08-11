"""Study rooms.

A room is one topic, for example "Grade 7 Algebra: Solving Equations". It is
created once and lives for weeks. All the turns, uploaded files and summaries for
that topic hang off it.

Design note worth defending — `archived_at` gives us soft delete.
Hard-deleting a room would cascade to its turns, messages and documents, which
would punch a hole in the student's study history, and "what did I study in July"
is a feature the brief asks for. Archiving removes the room from the active list
and leaves the record intact. NULL means active; a timestamp records when it was
archived.
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, String, desc, func, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, CreatedAtMixin, UpdatedAtMixin, UUIDPrimaryKey

if TYPE_CHECKING:
    from app.models.student import Student


class Room(UUIDPrimaryKey, CreatedAtMixin, UpdatedAtMixin, Base):
    __tablename__ = "rooms"

    student_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("students.id", ondelete="CASCADE"),
        nullable=False,
    )

    title: Mapped[str] = mapped_column(String(200), nullable=False)

    # NULL = active. Set by DELETE /rooms/{id}.
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Bumped on every new turn, so the room list shows the most recently used
    # room first.
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    student: Mapped["Student"] = relationship(back_populates="rooms")

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None

    __table_args__ = (
        # Serves the room list: filter by student, sort by recent activity.
        #
        # student_id first because we filter on it; last_activity_at second and
        # descending because we sort on it. The wrong order would force Postgres
        # to sort the result instead of walking the index.
        #
        # Partial (WHERE archived_at IS NULL) because the list never asks for
        # archived rooms. They never enter the index, so it stays small however
        # many rooms a student retires over the years.
        Index(
            "ix_rooms_student_active",
            "student_id",
            desc("last_activity_at"),
            postgresql_where=text("archived_at IS NULL"),
        ),
    )
