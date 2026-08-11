"""Request and response shapes for rooms."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.student import normalise_text


class RoomCreate(BaseModel):
    title: str = Field(
        min_length=1,
        max_length=200,
        description=(
            "What the student is studying. Shown in their room list, and given "
            "to the tutor as context, so the topic's depth is carried here."
        ),
        examples=["Grade 7 Algebra: Solving Equations", "AP Biology: Cell Respiration"],
    )

    @field_validator("title")
    @classmethod
    def _normalise(cls, value: str) -> str:
        cleaned = normalise_text(value)
        if not cleaned:
            raise ValueError("title cannot be blank")
        return cleaned


class RoomRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    archived_at: datetime | None = Field(
        default=None,
        description="Null while the room is active. Set when the room is deleted.",
    )
    last_activity_at: datetime
    created_at: datetime
