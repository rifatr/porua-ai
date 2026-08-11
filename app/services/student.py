"""Student business logic.

Small on purpose. The brief excludes a login flow, so this exists only to create
the demo students that the X-Student-Id header points at.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.student import Student
from app.schemas.student import StudentCreate


async def create_student(db: AsyncSession, data: StudentCreate) -> Student:
    student = Student(
        display_name=data.display_name,
        education_level=data.education_level,
    )
    db.add(student)
    await db.flush()
    await db.refresh(student)
    return student
