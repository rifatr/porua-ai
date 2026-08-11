"""Request and response shapes for students.

These are Pydantic models, not database models. Keeping the two apart means the
API contract does not shift every time a column changes, and internal columns are
never exposed by accident.
"""

import re
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

_WHITESPACE = re.compile(r"\s+")


def normalise_text(value: str) -> str:
    """Trim, and collapse runs of whitespace into a single space.

    "  Class   8 " and "Class 8" should not be two different values.
    """
    return _WHITESPACE.sub(" ", value).strip()


class StudentCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=120, examples=["Rifat"])
    education_level: str = Field(
        min_length=1,
        max_length=32,
        description=(
            "How advanced this student is. Free text, because school systems "
            "differ and a fixed list would have no valid value for a university "
            "student. Every tutor prompt reads it."
        ),
        examples=["Class 8", "Grade 7", "HSC 1st year", "University, 2nd year"],
    )

    @field_validator("display_name", "education_level")
    @classmethod
    def _normalise(cls, value: str) -> str:
        cleaned = normalise_text(value)
        if not cleaned:
            raise ValueError("cannot be blank")
        return cleaned


class StudentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    display_name: str
    education_level: str
    created_at: datetime
