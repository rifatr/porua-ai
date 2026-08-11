"""Students.

The brief says a login flow is not required, but it also asks to "query a
student's study history". So identity is modelled properly and the caller passes a
student id in the X-Student-Id header. Adding real authentication later means
replacing one dependency, not changing the schema.
"""

from typing import TYPE_CHECKING

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, CreatedAtMixin, UpdatedAtMixin, UUIDPrimaryKey

if TYPE_CHECKING:
    from app.models.room import Room


class Student(UUIDPrimaryKey, CreatedAtMixin, UpdatedAtMixin, Base):
    __tablename__ = "students"

    display_name: Mapped[str] = mapped_column(String(120), nullable=False)

    # Examples: "Class 8", "Grade 7", "HSC 1st year", "University, 2nd year",
    # "A-Level", "AP". Normalised (trimmed, whitespace collapsed) on the way in.
    education_level: Mapped[str] = mapped_column(String(32), nullable=False)

    rooms: Mapped[list["Room"]] = relationship(
        back_populates="student",
        cascade="all, delete-orphan",
    )
