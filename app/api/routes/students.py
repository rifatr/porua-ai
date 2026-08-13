"""Student endpoints.

The brief excludes a login flow, so there is no signup or session here. This
exists so you can create a student to put in the `X-Student-Id` header.

Study history lives here too. It reads as a property of a student, and its path
sits under `/students`, so a separate module would split one idea across two files
for no gain. The query itself is in `services/study_history.py`, shared with the
agent tool that answers the same question.
"""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.deps import CurrentStudent, DbSession
from app.schemas.student import StudentCreate, StudentRead
from app.schemas.study_history import StudyHistoryRead
from app.services import student as student_service
from app.services import study_history as history_service

router = APIRouter(prefix="/students", tags=["students"])


@router.post(
    "",
    response_model=StudentRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a student",
    description=(
        "Stands in for signing up. Put the returned `id` in the `X-Student-Id` "
        "header on every other request.\n\n"
        "`education_level` is required because every tutor prompt reads it — it "
        "is what makes answers pitched at the right level."
    ),
)
async def create_student(data: StudentCreate, db: DbSession) -> StudentRead:
    student = await student_service.create_student(db, data)
    return StudentRead.model_validate(student)


@router.get(
    "/me",
    response_model=StudentRead,
    summary="Who am I?",
    description="Echoes back the student the `X-Student-Id` header resolved to.",
)
async def get_me(student: CurrentStudent) -> StudentRead:
    return StudentRead.model_validate(student)


@router.get(
    "/me/study-history",
    response_model=StudyHistoryRead,
    summary="What did I study, and when",
    description=(
        "Rooms worked in and concepts covered between two dates.\n\n"
        "Counts only turns that **succeeded** and were **in scope**. A failed turn "
        "taught nothing, and an off-topic question is not revision of the room's "
        "subject — counting either would quietly inflate every number here.\n\n"
        "Both dates are optional: omit `from_date` for the last 30 days, omit "
        "`to_date` for 'up to today'. `to_date` is inclusive.\n\n"
        "The AI tutor reads the same data through the `query_study_history` tool, "
        "over this exact service — so the two can never disagree."
    ),
)
async def get_study_history(
    db: DbSession,
    student: CurrentStudent,
    from_date: Annotated[
        date | None,
        Query(description="Start of the range. Defaults to 30 days ago."),
    ] = None,
    to_date: Annotated[
        date | None,
        Query(description="End of the range, inclusive. Defaults to today."),
    ] = None,
) -> StudyHistoryRead:
    # Scoped to the caller, like every other read. The path says `/me` rather than
    # `/{student_id}` for that reason: an id in the path would invite passing
    # someone else's, and then the only thing standing between a student and
    # another student's history would be a check somebody has to remember to write.
    history = await history_service.get_study_history(
        db, student, from_date=from_date, to_date=to_date
    )
    return StudyHistoryRead.model_validate(history)
