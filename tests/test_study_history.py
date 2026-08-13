"""Study history, through the endpoint.

The brief lists this as an acceptance item and separately gives a past-chat query
as its example tool. Both read this one service, so these tests cover the tool's
data too — see `test_tools.py` for the tool wrapper itself.

Most of these are about what history must *not* count. Getting the totals right is
easy; getting them right in the presence of failed turns, off-topic turns, another
student's turns and a date boundary is the actual work, and each of those wrong
would produce plausible numbers nobody would question.
"""

from datetime import UTC, date, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.room import Room
from app.models.student import Student
from app.models.turn import Turn, TurnFailureReason, TurnStatus
from app.services.study_history import TOP_CONCEPTS, TOP_ROOMS


async def add_room(db: AsyncSession, student: Student, title: str) -> Room:
    room = Room(student_id=student.id, title=title)
    db.add(room)
    await db.flush()
    return room


async def add_turn(
    db: AsyncSession,
    room: Room,
    seq: int,
    *,
    days_ago: int = 1,
    concepts: list[str] | None = None,
    status: str = TurnStatus.SUCCEEDED.value,
    in_scope: bool | None = True,
) -> Turn:
    """A turn written straight to the database.

    Deliberately not through the API. These tests are about the *query*, and going
    through a turn would drag the whole model pipeline in and make the dates
    impossible to control.
    """
    when = datetime.now(UTC) - timedelta(days=days_ago)
    turn = Turn(
        room_id=room.id,
        seq=seq,
        student_message=f"question {seq}",
        answer_text="answer" if status == TurnStatus.SUCCEEDED.value else None,
        failure_reason=(
            None
            if status == TurnStatus.SUCCEEDED.value
            else TurnFailureReason.PROVIDER_UNAVAILABLE.value
        ),
        status=status,
        in_scope=in_scope if status == TurnStatus.SUCCEEDED.value else None,
        concepts=concepts if concepts is not None else [],
        created_at=when,
        completed_at=when,
    )
    db.add(turn)
    await db.flush()
    return turn


async def history(client: AsyncClient, auth: dict[str, str], query: str = "") -> dict:
    response = await client.get(f"/students/me/study-history{query}", headers=auth)
    assert response.status_code == 200, response.text
    return response.json()


# --- the basics -------------------------------------------------------------


async def test_a_new_student_has_an_empty_history(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    """Empty, not an error. Nothing studied yet is a valid answer."""
    body = await history(client, auth)
    assert body["total_turns"] == 0
    assert body["rooms"] == []
    assert body["concepts"] == []


async def test_rooms_and_turns_are_counted(
    client: AsyncClient, auth: dict[str, str], db: AsyncSession, student: Student
) -> None:
    algebra = await add_room(db, student, "Algebra")
    biology = await add_room(db, student, "Biology")
    for seq in (1, 2, 3):
        await add_turn(db, algebra, seq)
    await add_turn(db, biology, 1)

    body = await history(client, auth)

    assert body["total_turns"] == 4
    assert {room["title"]: room["turns"] for room in body["rooms"]} == {
        "Algebra": 3,
        "Biology": 1,
    }


async def test_concepts_are_counted_across_rooms(
    client: AsyncClient, auth: dict[str, str], db: AsyncSession, student: Student
) -> None:
    """This is what makes history answer "what did I study" rather than only
    "which rooms did I open"."""
    algebra = await add_room(db, student, "Algebra")
    revision = await add_room(db, student, "Revision")
    await add_turn(db, algebra, 1, concepts=["linear equations", "inverse operations"])
    await add_turn(db, algebra, 2, concepts=["linear equations"])
    await add_turn(db, revision, 1, concepts=["linear equations", "pythagoras"])

    body = await history(client, auth)

    assert body["concepts"][0] == {"concept": "linear equations", "turns": 3}
    assert {item["concept"] for item in body["concepts"]} == {
        "linear equations",
        "inverse operations",
        "pythagoras",
    }


async def test_rooms_come_back_most_recent_first(
    client: AsyncClient, auth: dict[str, str], db: AsyncSession, student: Student
) -> None:
    old = await add_room(db, student, "Old")
    new = await add_room(db, student, "New")
    await add_turn(db, old, 1, days_ago=20)
    await add_turn(db, new, 1, days_ago=1)

    body = await history(client, auth)
    assert [room["title"] for room in body["rooms"]] == ["New", "Old"]


# --- what must not be counted -----------------------------------------------


async def test_failed_turns_are_not_study_time(
    client: AsyncClient, auth: dict[str, str], db: AsyncSession, student: Student
) -> None:
    """A turn that never produced an answer taught nothing."""
    room = await add_room(db, student, "Algebra")
    await add_turn(db, room, 1, concepts=["linear equations"])
    await add_turn(db, room, 2, status=TurnStatus.FAILED.value)

    body = await history(client, auth)
    assert body["total_turns"] == 1


async def test_off_topic_turns_are_not_study_time(
    client: AsyncClient, auth: dict[str, str], db: AsyncSession, student: Student
) -> None:
    """Asking about football in an algebra room is not algebra revision.

    This is the reader `in_scope` was stored for. Without it, a student who
    chatted about the World Cup would appear to have revised algebra.
    """
    room = await add_room(db, student, "Algebra")
    await add_turn(db, room, 1, concepts=["linear equations"])
    await add_turn(db, room, 2, in_scope=False, concepts=[])

    body = await history(client, auth)
    assert body["total_turns"] == 1
    assert len(body["rooms"]) == 1


async def test_another_students_history_is_invisible(
    client: AsyncClient, auth: dict[str, str], db: AsyncSession, student: Student
) -> None:
    other = Student(display_name="Someone Else", education_level="Class 9")
    db.add(other)
    await db.flush()
    their_room = await add_room(db, other, "Their Room")
    await add_turn(db, their_room, 1, concepts=["their secret topic"])

    body = await history(client, auth)

    assert body["total_turns"] == 0
    assert body["rooms"] == []
    assert body["concepts"] == []


async def test_turns_with_no_concepts_still_count_as_turns(
    client: AsyncClient, auth: dict[str, str], db: AsyncSession, student: Student
) -> None:
    """A greeting is a turn but not a topic. It should not invent a blank tag."""
    room = await add_room(db, student, "Algebra")
    await add_turn(db, room, 1, concepts=[])

    body = await history(client, auth)
    assert body["total_turns"] == 1
    assert body["concepts"] == []


# --- the date range ---------------------------------------------------------


async def test_the_default_window_is_the_last_thirty_days(
    client: AsyncClient, auth: dict[str, str], db: AsyncSession, student: Student
) -> None:
    room = await add_room(db, student, "Algebra")
    await add_turn(db, room, 1, days_ago=5)
    await add_turn(db, room, 2, days_ago=200)

    body = await history(client, auth)

    assert body["total_turns"] == 1
    assert body["from_date"] == (date.today() - timedelta(days=30)).isoformat()


async def test_an_explicit_range_is_honoured(
    client: AsyncClient, auth: dict[str, str], db: AsyncSession, student: Student
) -> None:
    room = await add_room(db, student, "Algebra")
    await add_turn(db, room, 1, days_ago=3)
    await add_turn(db, room, 2, days_ago=40)

    start = (date.today() - timedelta(days=60)).isoformat()
    body = await history(client, auth, f"?from_date={start}")

    assert body["total_turns"] == 2


async def test_today_is_included(
    client: AsyncClient, auth: dict[str, str], db: AsyncSession, student: Student
) -> None:
    """`to_date` is inclusive, and `created_at` is a timestamp.

    Comparing a timestamp to a bare date would silently drop everything studied
    after midnight — which is all of today's work.
    """
    room = await add_room(db, student, "Algebra")
    await add_turn(db, room, 1, days_ago=0)

    body = await history(client, auth, f"?to_date={date.today().isoformat()}")
    assert body["total_turns"] == 1


async def test_a_backwards_range_is_rejected(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    response = await client.get(
        "/students/me/study-history?from_date=2026-08-01&to_date=2026-07-01",
        headers=auth,
    )
    assert response.status_code == 422
    assert "from_date" in response.json()["detail"]


async def test_an_unbounded_range_is_rejected(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    """Not politeness — an open range invites a scan of every turn ever taken."""
    response = await client.get(
        "/students/me/study-history?from_date=1990-01-01", headers=auth
    )
    assert response.status_code == 422
    assert "366" in response.json()["detail"]


# --- the caps ---------------------------------------------------------------


async def test_rooms_are_capped_at_the_most_recent(
    client: AsyncClient, auth: dict[str, str], db: AsyncSession, student: Student
) -> None:
    """The date range alone does not bound the result — inside one year a student
    can open any number of rooms. The cap keeps what they are working on now."""
    for index in range(TOP_ROOMS + 5):
        # Room 0 studied longest ago, so the five oldest fall off the end.
        room = await add_room(db, student, f"Room {index}")
        await add_turn(db, room, 1, days_ago=TOP_ROOMS + 5 - index)

    # An explicit range, because one room per day runs past the 30-day default.
    since = (date.today() - timedelta(days=90)).isoformat()
    body = await history(client, auth, f"?from_date={since}")

    assert len(body["rooms"]) == TOP_ROOMS
    titles = [room["title"] for room in body["rooms"]]
    assert titles[0] == f"Room {TOP_ROOMS + 4}"
    assert "Room 0" not in titles
    # One turn per room, and the total follows the rooms returned rather than
    # counting the ones the cap dropped.
    assert body["total_turns"] == TOP_ROOMS


async def test_concepts_are_capped_at_the_most_studied(
    client: AsyncClient, auth: dict[str, str], db: AsyncSession, student: Student
) -> None:
    room = await add_room(db, student, "Everything")
    for index in range(TOP_CONCEPTS + 5):
        # Concept 0 tagged once, concept 1 twice, and so on — so the rarest are
        # the ones the cap should drop.
        for seq in range(index + 1):
            await add_turn(
                db, room, index * 100 + seq + 1, concepts=[f"concept {index}"]
            )

    body = await history(client, auth)

    assert len(body["concepts"]) == TOP_CONCEPTS
    returned = {item["concept"] for item in body["concepts"]}
    assert f"concept {TOP_CONCEPTS + 4}" in returned
    assert "concept 0" not in returned
