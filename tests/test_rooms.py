"""Room endpoints.

The interesting tests here are not "does create work" — they are ownership
(§S4 depends on it) and cursor paging (the "as history grows" claim).
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.room import Room
from app.models.student import Student


async def make_room(
    db: AsyncSession,
    student: Student,
    title: str,
    *,
    minutes_ago: int = 0,
    archived: bool = False,
) -> Room:
    """Create a room with an explicit activity time.

    Timestamps are set by hand because Postgres' now() returns the transaction
    start time — every row created inside one test would otherwise share the same
    value, and ordering assertions would be meaningless.
    """
    when = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    room = Room(
        student_id=student.id,
        title=title,
        last_activity_at=when,
        archived_at=when if archived else None,
    )
    db.add(room)
    await db.flush()
    return room


# --- create -----------------------------------------------------------------


async def test_create_room(client: AsyncClient, auth: dict[str, str]) -> None:
    response = await client.post(
        "/rooms",
        json={"title": "Grade 7 Algebra: Solving Equations"},
        headers=auth,
    )
    assert response.status_code == 201

    body = response.json()
    assert body["title"] == "Grade 7 Algebra: Solving Equations"
    assert body["archived_at"] is None
    assert body["id"] and body["created_at"] and body["last_activity_at"]


async def test_create_room_normalises_the_title(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    response = await client.post(
        "/rooms", json={"title": "  Grade 7   Algebra  "}, headers=auth
    )
    assert response.status_code == 201
    assert response.json()["title"] == "Grade 7 Algebra"


async def test_create_room_rejects_a_blank_title(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    response = await client.post("/rooms", json={"title": "   "}, headers=auth)
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_FAILED"


async def test_create_room_requires_the_student_header(client: AsyncClient) -> None:
    response = await client.post("/rooms", json={"title": "Anything"})
    assert response.status_code == 422


async def test_create_room_rejects_an_unknown_student(client: AsyncClient) -> None:
    response = await client.post(
        "/rooms", json={"title": "Anything"}, headers={"X-Student-Id": str(uuid4())}
    )
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


# --- list -------------------------------------------------------------------


async def test_list_rooms_puts_the_most_recent_first(
    client: AsyncClient, db: AsyncSession, student: Student, auth: dict[str, str]
) -> None:
    await make_room(db, student, "oldest", minutes_ago=30)
    await make_room(db, student, "newest", minutes_ago=1)
    await make_room(db, student, "middle", minutes_ago=10)

    response = await client.get("/rooms", headers=auth)

    assert response.status_code == 200
    assert [r["title"] for r in response.json()["items"]] == [
        "newest",
        "middle",
        "oldest",
    ]


async def test_list_rooms_hides_archived_by_default(
    client: AsyncClient, db: AsyncSession, student: Student, auth: dict[str, str]
) -> None:
    await make_room(db, student, "active", minutes_ago=1)
    await make_room(db, student, "deleted", minutes_ago=2, archived=True)

    titles = [r["title"] for r in (await client.get("/rooms", headers=auth)).json()["items"]]
    assert titles == ["active"]

    with_archived = await client.get("/rooms?include_archived=true", headers=auth)
    assert {r["title"] for r in with_archived.json()["items"]} == {"active", "deleted"}


async def test_list_rooms_only_shows_your_own(
    client: AsyncClient, db: AsyncSession, student: Student, auth: dict[str, str]
) -> None:
    other = Student(display_name="Someone Else", education_level="Class 9")
    db.add(other)
    await db.flush()
    await make_room(db, other, "not yours")
    await make_room(db, student, "yours")

    titles = [r["title"] for r in (await client.get("/rooms", headers=auth)).json()["items"]]
    assert titles == ["yours"]


async def test_cursor_paging_walks_every_room_exactly_once(
    client: AsyncClient, db: AsyncSession, student: Student, auth: dict[str, str]
) -> None:
    expected = [f"room-{n}" for n in range(5)]
    for index, title in enumerate(expected):
        await make_room(db, student, title, minutes_ago=index)

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(10):  # guard against an endless loop if paging is broken
        url = f"/rooms?limit=2{f'&cursor={cursor}' if cursor else ''}"
        page = (await client.get(url, headers=auth)).json()
        seen.extend(r["title"] for r in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert seen == expected, "paging must not duplicate or skip rows"
    assert len(seen) == len(set(seen))


async def test_a_bad_cursor_is_rejected(client: AsyncClient, auth: dict[str, str]) -> None:
    response = await client.get("/rooms?cursor=garbage", headers=auth)
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_FAILED"


async def test_limit_is_capped(client: AsyncClient, auth: dict[str, str]) -> None:
    response = await client.get("/rooms?limit=5000", headers=auth)
    assert response.status_code == 422


# --- get --------------------------------------------------------------------


async def test_get_room(
    client: AsyncClient, db: AsyncSession, student: Student, auth: dict[str, str]
) -> None:
    room = await make_room(db, student, "Cell Respiration")
    response = await client.get(f"/rooms/{room.id}", headers=auth)
    assert response.status_code == 200
    assert response.json()["title"] == "Cell Respiration"


async def test_another_students_room_is_not_found_rather_than_forbidden(
    client: AsyncClient, db: AsyncSession, auth: dict[str, str]
) -> None:
    """404, not 403 — a 403 would confirm that the id exists."""
    other = Student(display_name="Someone Else", education_level="Class 9")
    db.add(other)
    await db.flush()
    room = await make_room(db, other, "private")

    response = await client.get(f"/rooms/{room.id}", headers=auth)
    assert response.status_code == 404


async def test_archived_rooms_are_still_readable(
    client: AsyncClient, db: AsyncSession, student: Student, auth: dict[str, str]
) -> None:
    room = await make_room(db, student, "deleted", archived=True)
    response = await client.get(f"/rooms/{room.id}", headers=auth)
    assert response.status_code == 200
    assert response.json()["archived_at"] is not None


# --- delete -----------------------------------------------------------------


async def test_delete_archives_instead_of_removing(
    client: AsyncClient, db: AsyncSession, student: Student, auth: dict[str, str]
) -> None:
    room = await make_room(db, student, "to delete")

    response = await client.delete(f"/rooms/{room.id}", headers=auth)
    assert response.status_code == 204

    await db.refresh(room)
    assert room.archived_at is not None, "the row must survive; only the flag changes"


async def test_delete_is_idempotent(
    client: AsyncClient, db: AsyncSession, student: Student, auth: dict[str, str]
) -> None:
    room = await make_room(db, student, "to delete")

    first = await client.delete(f"/rooms/{room.id}", headers=auth)
    second = await client.delete(f"/rooms/{room.id}", headers=auth)

    assert first.status_code == second.status_code == 204

    await db.refresh(room)
    archived_at = room.archived_at
    assert archived_at is not None

    # The second delete must not move the timestamp.
    await client.delete(f"/rooms/{room.id}", headers=auth)
    await db.refresh(room)
    assert room.archived_at == archived_at


async def test_cannot_delete_another_students_room(
    client: AsyncClient, db: AsyncSession, auth: dict[str, str]
) -> None:
    other = Student(display_name="Someone Else", education_level="Class 9")
    db.add(other)
    await db.flush()
    room = await make_room(db, other, "private")

    response = await client.delete(f"/rooms/{room.id}", headers=auth)
    assert response.status_code == 404

    await db.refresh(room)
    assert room.archived_at is None
