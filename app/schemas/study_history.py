"""The study-history response shape.

The two caps are imported from the service rather than written out again here.
They shape the SQL there and the documentation here, and a number copied into a
description is a number that goes stale the first time someone tunes it.
"""

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.services.study_history import TOP_CONCEPTS, TOP_ROOMS


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
    total_turns: int = Field(
        description="Turns across the rooms listed below. If `rooms` hit its cap, "
        "this counts those rooms only, not every room in the range."
    )
    rooms: list[RoomActivityRead] = Field(
        description=f"Most recently studied first, at most {TOP_ROOMS}."
    )
    concepts: list[ConceptCountRead] = Field(
        description=f"Most studied first, at most {TOP_CONCEPTS}. This is what "
        "makes the history answer 'what did I study' rather than only 'which "
        "rooms did I open'."
    )
