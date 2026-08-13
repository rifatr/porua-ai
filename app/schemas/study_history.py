"""The study-history response shape."""

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RoomActivityRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    room_id: UUID
    title: str
    turns: int = Field(description="Questions asked in this room during the range.")
    last_studied_at: datetime


class ConceptCountRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    concept: str
    turns: int = Field(description="How many turns taught this concept.")


class StudyHistoryRead(BaseModel):
    """What a student studied between two dates.

    Counts only turns that succeeded and were in scope. A failed turn taught
    nothing, and an off-topic question is not revision of the room's subject —
    including either would quietly inflate every number here.
    """

    model_config = ConfigDict(from_attributes=True)

    from_date: date
    to_date: date = Field(description="Inclusive.")
    total_turns: int
    rooms: list[RoomActivityRead]
    concepts: list[ConceptCountRead] = Field(
        description="Most studied first. This is what makes the history answer "
        "'what did I study' rather than only 'which rooms did I open'."
    )
