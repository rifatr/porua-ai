"""Room business logic.

Every function here takes the resolved `Student` object, never a raw id from a
request body, and every query filters on `student_id`. That is deliberate, and it
is the whole authorisation story for this project:

    a caller can only reach rows belonging to the student the request resolved to.

In slice S4 the agent tools call into this same layer. Because the student comes
from the request context rather than from a parameter, there is no argument the
language model could set to reach another student's data.
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import NotFoundError
from app.api.pagination import apply_cursor
from app.models.room import Room
from app.models.student import Student
from app.schemas.room import RoomCreate


async def create_room(db: AsyncSession, student: Student, data: RoomCreate) -> Room:
    room = Room(student_id=student.id, title=data.title)
    db.add(room)
    # flush sends the INSERT so Postgres fills in id, created_at and
    # last_activity_at; refresh reads those server-generated values back.
    await db.flush()
    await db.refresh(room)
    return room


async def list_rooms(
    db: AsyncSession,
    student: Student,
    *,
    cursor: str | None,
    limit: int,
    include_archived: bool = False,
) -> list[Room]:
    """This student's rooms, most recently used first.

    Returns up to limit + 1 rows. The caller trims the extra one and turns it
    into the next cursor, which avoids a second COUNT query over the table.
    """
    stmt = select(Room).where(Room.student_id == student.id)
    if not include_archived:
        # Matches the partial index exactly, so this stays an index walk.
        stmt = stmt.where(Room.archived_at.is_(None))

    stmt = apply_cursor(
        stmt,
        sort_column=Room.last_activity_at,
        id_column=Room.id,
        cursor=cursor,
        limit=limit,
    )
    return list((await db.execute(stmt)).scalars().all())


async def get_room(db: AsyncSession, student: Student, room_id: UUID) -> Room:
    """Fetch one room owned by this student.

    Someone else's room returns 404, not 403. Returning "forbidden" would confirm
    the id exists, and there is no reason to give that away.

    Archived rooms are still readable. You cannot find them in the list, but a
    saved link keeps working: deleting a room hides it, it does not erase it.
    """
    stmt = select(Room).where(Room.id == room_id, Room.student_id == student.id)
    room = (await db.execute(stmt)).scalar_one_or_none()
    if room is None:
        raise NotFoundError(f"No room with id {room_id}.")
    return room


async def archive_room(db: AsyncSession, student: Student, room_id: UUID) -> Room:
    """Soft delete.

    We never hard-delete a room. The cascade would take its turns, messages and
    uploaded files with it, leaving a hole in the study history — and "what did I
    study in July" is a feature the brief asks for.

    Idempotent: archiving an already-archived room is a no-op, so a client
    retrying a request that actually succeeded does not get an error.
    """
    room = await get_room(db, student, room_id)
    if room.archived_at is None:
        room.archived_at = datetime.now(UTC)
        await db.flush()
    return room


async def touch_room(db: AsyncSession, room: Room) -> None:
    """Mark the room as just used, so it sorts to the top of the list.

    Called by S2 whenever a turn is created.
    """
    room.last_activity_at = datetime.now(UTC)
    await db.flush()
