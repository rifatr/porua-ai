"""Room endpoints.

A room is one topic the student is studying. It is created once and lives for
weeks; the turns inside it are the individual questions and answers.
"""

from uuid import UUID

from fastapi import APIRouter, Query, Response, status

from app.api.deps import CurrentStudent, DbSession, PageLimit
from app.api.pagination import Page, build_page
from app.schemas.room import RoomCreate, RoomRead
from app.services import room as room_service

router = APIRouter(prefix="/rooms", tags=["rooms"])


@router.post(
    "",
    response_model=RoomRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a study room",
    description=(
        "Creates a room for one topic. No AI call happens here — this is a plain "
        "database write, and it is instant. Talking to the tutor is "
        "`POST /rooms/{room_id}/turns`."
    ),
)
async def create_room(
    data: RoomCreate,
    db: DbSession,
    student: CurrentStudent,
) -> RoomRead:
    room = await room_service.create_room(db, student, data)
    return RoomRead.model_validate(room)


@router.get(
    "",
    response_model=Page[RoomRead],
    summary="List the student's rooms",
    description=(
        "Most recently used first. Archived rooms are hidden unless you ask for "
        "them.\n\n"
        "Paging is cursor-based: pass the `next_cursor` from one response back as "
        "`cursor` to get the next page. `next_cursor` is null on the last page. "
        "There are no page numbers, because `OFFSET` reads and discards every "
        "skipped row and so gets slower as a student's history grows."
    ),
)
async def list_rooms(
    db: DbSession,
    student: CurrentStudent,
    limit: PageLimit,
    cursor: str | None = Query(
        default=None,
        description="The `next_cursor` value from the previous response.",
    ),
    include_archived: bool = Query(
        default=False,
        description="Include rooms that have been deleted.",
    ),
) -> Page[RoomRead]:
    rows = await room_service.list_rooms(
        db,
        student,
        cursor=cursor,
        limit=limit,
        include_archived=include_archived,
    )
    return build_page(
        rows,
        limit=limit,
        sort_attr="last_activity_at",
        serializer=RoomRead,
    )


@router.get(
    "/{room_id}",
    response_model=RoomRead,
    summary="Open one room",
    description=(
        "Works for archived rooms too — deleting hides a room from the list, it "
        "does not erase it."
    ),
)
async def get_room(
    room_id: UUID,
    db: DbSession,
    student: CurrentStudent,
) -> RoomRead:
    room = await room_service.get_room(db, student, room_id)
    return RoomRead.model_validate(room)


@router.delete(
    "/{room_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a room (soft delete)",
    description=(
        "Marks the room archived and removes it from the room list. Nothing is "
        "erased: its turns, messages and uploaded files stay, so study history "
        "for that period stays correct.\n\n"
        "Idempotent — deleting an already-deleted room succeeds again."
    ),
)
async def delete_room(
    room_id: UUID,
    db: DbSession,
    student: CurrentStudent,
) -> Response:
    await room_service.archive_room(db, student, room_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
