"""Shared FastAPI dependencies.

A dependency is a function FastAPI runs before your endpoint. You declare it with
`Depends(...)` and FastAPI passes the result in as an argument. It is how we avoid
repeating session handling and student lookup in every single endpoint.
"""

from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import NotFoundError, ValidationFailedError
from app.config import Settings, get_settings
from app.db.session import get_session
from app.llm import LLMClient, get_llm_client
from app.models.student import Student

DbSession = Annotated[AsyncSession, Depends(get_session)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def get_llm() -> LLMClient:
    """The language model client, as a dependency.

    Wrapping the factory in a dependency is what lets tests swap in a fake with
    `app.dependency_overrides`, so the suite never touches the network. S3 will
    replace the object this returns with a wrapped, retrying, validating version
    without any endpoint changing.
    """
    return get_llm_client()


LLM = Annotated[LLMClient, Depends(get_llm)]


async def current_student(
    db: DbSession,
    x_student_id: Annotated[
        UUID,
        Header(
            description="Id of the student making the request. "
            "Stands in for authentication, which the brief excludes.",
        ),
    ],
) -> Student:
    """Resolve the caller from the X-Student-Id header.

    This is the single place identity enters the system. Every query and every
    agent tool takes the student from here, never from a request body and never
    from anything the language model produced. That is what stops a tool from
    reading another student's data: the model has no way to supply a student id.

    Replacing this function with a real token check is the whole auth story.
    """
    student = await db.get(Student, x_student_id)
    if student is None:
        raise NotFoundError(f"No student with id {x_student_id}.")
    return student


CurrentStudent = Annotated[Student, Depends(current_student)]


def page_limit(
    settings: AppSettings,
    limit: Annotated[int | None, Query(ge=1, description="Rows per page.")] = None,
) -> int:
    """Clamp the page size so a client cannot ask for the entire table."""
    if limit is None:
        return settings.default_page_size
    if limit > settings.max_page_size:
        raise ValidationFailedError(f"limit cannot be greater than {settings.max_page_size}.")
    return limit


PageLimit = Annotated[int, Depends(page_limit)]
