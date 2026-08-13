"""What a student studied, and when.

One service, two callers. The brief lists study history as an acceptance item
(*"query a student's study history over a period of time"*) and separately gives a
past-chat query as its example tool. Those are the same question asked through two
doors, so the query lives here and both the HTTP endpoint and the agent tool call
it. Writing it twice would let the two answers drift, and then "what did I study"
would depend on who was asking.

## What counts as studying

Only turns that **succeeded** and were **in scope**. A failed turn taught nothing.
An off-topic turn — the student asking about football in an algebra room — is not
algebra revision, and counting it would quietly inflate every number here. This is
the reader that `in_scope` was stored for.

## Why concepts are counted here rather than stored as counts

`turns.concepts` holds the tags for one turn. Totals are derived from them on
demand with a `GROUP BY`. Storing running totals somewhere would be a second copy
of a fact already recorded, and second copies drift — a turn deleted or re-tagged
would leave the total wrong with nothing to notice it.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID

from sqlalchemy import func, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import ValidationFailedError
from app.models.room import Room
from app.models.student import Student
from app.models.turn import Turn, TurnStatus

DEFAULT_WINDOW_DAYS = 30
MAX_WINDOW_DAYS = 366
TOP_CONCEPTS = 15


@dataclass(frozen=True)
class ConceptCount:
    concept: str
    turns: int


@dataclass(frozen=True)
class RoomActivity:
    room_id: UUID
    title: str
    turns: int
    last_studied_at: datetime


@dataclass(frozen=True)
class StudyHistory:
    from_date: date
    to_date: date
    total_turns: int
    rooms: list[RoomActivity]
    concepts: list[ConceptCount]


def resolve_range(from_date: date | None, to_date: date | None) -> tuple[date, date]:
    """Fill in the missing end of a range, and refuse a nonsensical one.

    Both ends optional so a caller can say "the last month" without doing date
    arithmetic. The upper bound on the window is not politeness — an unbounded
    range invites a query that scans every turn a student has ever taken.
    """
    today = datetime.now(UTC).date()
    to_date = to_date or today
    from_date = from_date or (to_date - timedelta(days=DEFAULT_WINDOW_DAYS))

    if from_date > to_date:
        raise ValidationFailedError("from_date cannot be after to_date.")
    if (to_date - from_date).days > MAX_WINDOW_DAYS:
        raise ValidationFailedError(
            f"The range cannot be longer than {MAX_WINDOW_DAYS} days."
        )

    return from_date, to_date


def _bounds(from_date: date, to_date: date) -> tuple[datetime, datetime]:
    """Dates in, timestamps out. `to_date` is inclusive for the caller.

    `created_at` is a timestamp, so comparing it to a bare date would silently drop
    everything studied later than midnight on the last day. Half-open at the top
    (`< to_date + 1 day`) rather than `<=` avoids the same class of bug at the
    other end, where `<=` on a timestamp misses the final second.
    """
    return (
        datetime.combine(from_date, time.min, tzinfo=UTC),
        datetime.combine(to_date + timedelta(days=1), time.min, tzinfo=UTC),
    )


async def get_study_history(
    db: AsyncSession,
    student: Student,
    *,
    from_date: date | None = None,
    to_date: date | None = None,
) -> StudyHistory:
    """Rooms and concepts a student worked on between two dates."""
    from_date, to_date = resolve_range(from_date, to_date)
    start, end = _bounds(from_date, to_date)

    # The join to rooms is what scopes this to one student. There is no path here
    # that takes a student id from anywhere but the caller.
    conditions = (
        Room.student_id == student.id,
        Turn.status == TurnStatus.SUCCEEDED.value,
        Turn.in_scope.is_(True),
        Turn.created_at >= start,
        Turn.created_at < end,
    )

    rooms_stmt = (
        select(
            Room.id,
            Room.title,
            func.count(Turn.id).label("turns"),
            func.max(Turn.created_at).label("last_studied_at"),
        )
        .select_from(Turn)
        .join(Room, Room.id == Turn.room_id)
        .where(*conditions)
        .group_by(Room.id, Room.title)
        .order_by(func.max(Turn.created_at).desc())
    )
    rooms = [
        RoomActivity(room_id=row.id, title=row.title, turns=row.turns,
                     last_studied_at=row.last_studied_at)
        for row in (await db.execute(rooms_stmt)).all()
    ]

    # Expands the jsonb array into one row per tag, so they can be grouped. A turn
    # with no concepts contributes no rows and drops out, which is what we want —
    # a greeting is not a topic.
    #
    # Written as an explicit `JOIN LATERAL ... ON true`. The shorter form,
    # `column_valued()`, produces identical SQL but leaves the expansion looking
    # like an unjoined table, and SQLAlchemy warns about a cartesian product on
    # every call. It is not one — the function reads `turns.concepts`, so it is
    # correlated per row — but a warning that is always wrong is a warning nobody
    # reads when it is finally right.
    # `render_derived` is what keeps the `(concept)` column alias. Without it the
    # expansion renders as a bare `AS anon_1`, the column takes the function's own
    # name, and `anon_1.concept` does not exist — SQL that compiles cleanly and
    # fails at the database.
    concept = (
        func.jsonb_array_elements_text(Turn.concepts)
        .table_valued("concept")
        .render_derived(name="tag", with_types=False)
        .lateral()
    )
    concepts_stmt = (
        select(concept.c.concept, func.count().label("turns"))
        .select_from(Turn)
        .join(Room, Room.id == Turn.room_id)
        .join(concept, true())
        .where(*conditions)
        .group_by(concept.c.concept)
        # Alphabetical after the count, so equal counts come back in a stable
        # order instead of whatever the planner felt like.
        .order_by(func.count().desc(), concept.c.concept)
        .limit(TOP_CONCEPTS)
    )
    concepts = [
        ConceptCount(concept=row.concept, turns=row.turns)
        for row in (await db.execute(concepts_stmt)).all()
    ]

    return StudyHistory(
        from_date=from_date,
        to_date=to_date,
        # Summed from the rooms rather than counted again. One less query, and the
        # two numbers cannot disagree.
        total_turns=sum(room.turns for room in rooms),
        rooms=rooms,
        concepts=concepts,
    )
