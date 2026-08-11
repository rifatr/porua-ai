"""Student endpoints.

The brief excludes a login flow, so there is no signup or session here. This
exists so you can create a student to put in the `X-Student-Id` header.
"""

from fastapi import APIRouter, status

from app.api.deps import CurrentStudent, DbSession
from app.schemas.student import StudentCreate, StudentRead
from app.services import student as student_service

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
