"""Running a turn.

Slice S2 on purpose does **no validation** of what the model returns. It calls
Gemini, records the attempt in full, and saves whatever came back.

That is not an oversight, it is the build order. S3 opens by pointing fixtures at
this code and showing that it happily returns junk, then builds the parse, schema
and content checks against failures we have actually observed rather than ones we
imagined. Recording attempts from the very first call is what makes those failures
visible in the first place.

What S2 *does* get right is the bookkeeping: every call produces a `turn_attempts`
row whether it succeeded or failed, and a turn that fails carries a typed reason
and no answer.
"""

import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import NotFoundError
from app.api.pagination import apply_cursor
from app.config import get_settings
from app.llm import LLMClient, LLMError, LLMRequest
from app.models.room import Room
from app.models.student import Student
from app.models.turn import Turn, TurnStatus
from app.models.turn_attempt import AttemptPurpose, TurnAttempt
from app.prompts.loader import load_prompt
from app.services import room as room_service

logger = logging.getLogger(__name__)

TUTOR_PROMPT = "tutor_system"
TUTOR_PROMPT_VERSION = 1

# How many earlier turns are replayed into the prompt.
#
# Fixed and small on purpose. Sending the whole room would make every request
# slower and more expensive as the room grows, which is precisely the "how does
# it behave as history grows" problem. The proper answer is a rolling summary
# (room_summaries, P2 #31); this is the honest interim one, and its limit is
# written down rather than hidden.
CONTEXT_TURN_COUNT = 6


async def _next_seq(db: AsyncSession, room: Room) -> int:
    """Next turn number for this room.

    A UNIQUE(room_id, seq) constraint backs this up. If two requests for the same
    room race, both may compute the same number and the second INSERT fails on the
    constraint rather than silently forking the conversation order.
    """
    current = await db.scalar(
        select(func.max(Turn.seq)).where(Turn.room_id == room.id)
    )
    return (current or 0) + 1


async def _recent_turns(db: AsyncSession, room: Room) -> list[Turn]:
    """The last few completed turns, oldest first for the prompt."""
    stmt = (
        select(Turn)
        .where(
            Turn.room_id == room.id,
            Turn.status == TurnStatus.SUCCEEDED.value,
        )
        .order_by(Turn.seq.desc())
        .limit(CONTEXT_TURN_COUNT)
    )
    rows = list((await db.execute(stmt)).scalars().all())
    return list(reversed(rows))


async def create_turn(
    db: AsyncSession,
    student: Student,
    room_id: UUID,
    message: str,
    llm: LLMClient,
) -> Turn:
    """Ask the tutor a question and record everything about the attempt."""
    room = await room_service.get_room(db, student, room_id)
    settings = get_settings()
    prompt = load_prompt(TUTOR_PROMPT, TUTOR_PROMPT_VERSION)

    turn = Turn(
        room_id=room.id,
        seq=await _next_seq(db, room),
        student_message=message,
        status=TurnStatus.RUNNING.value,
    )
    db.add(turn)
    await db.flush()

    rendered = prompt.render(
        education_level=student.education_level,
        room_title=room.title,
        recent_turns=await _recent_turns(db, room),
        student_message=message,
    )

    request = LLMRequest(
        prompt=rendered,
        model=settings.gemini_model,
        temperature=prompt.temperature,
    )

    attempt = TurnAttempt(
        turn_id=turn.id,
        attempt_no=1,
        purpose=AttemptPurpose.TUTOR.value,
        prompt_name=prompt.name,
        prompt_version=prompt.version,
        prompt_sha256=prompt.sha256,
        rendered_prompt=rendered,
        request_params=request.as_params(),
        started_at=datetime.now(UTC),
    )
    db.add(attempt)
    # Written before the call, so a crash mid-request still leaves evidence that
    # the call was made and what was sent.
    await db.flush()

    try:
        response = await llm.generate(request)
    except LLMError as exc:
        _finish_attempt(attempt, error_type=exc.error_type, error_detail=exc.detail)
        _finish_turn(turn, failure_reason=exc.error_type)
        await db.flush()
        await db.refresh(turn)
        logger.warning("turn %s failed: %s", turn.id, exc.error_type)
        return turn

    _finish_attempt(
        attempt,
        raw_response=response.raw,
        finish_reason=response.finish_reason,
        prompt_tokens=response.prompt_tokens,
        output_tokens=response.output_tokens,
        thought_tokens=response.thought_tokens,
    )

    # S2 saves the text unchecked. S3 inserts the validation pipeline here.
    turn.answer_text = response.text
    _finish_turn(turn)

    await room_service.touch_room(db, room)
    await db.flush()
    # created_at is filled in by Postgres, so it has to be read back before
    # duration_ms can be computed from it.
    await db.refresh(turn)
    return turn


def _finish_attempt(attempt: TurnAttempt, **fields) -> None:
    attempt.finished_at = datetime.now(UTC)
    for key, value in fields.items():
        setattr(attempt, key, value)


def _finish_turn(turn: Turn, failure_reason: str | None = None) -> None:
    turn.completed_at = datetime.now(UTC)
    if failure_reason:
        turn.status = TurnStatus.FAILED.value
        turn.failure_reason = failure_reason
    else:
        turn.status = TurnStatus.SUCCEEDED.value


async def list_turns(
    db: AsyncSession,
    student: Student,
    room_id: UUID,
    *,
    cursor: str | None,
    limit: int,
) -> list[Turn]:
    room = await room_service.get_room(db, student, room_id)
    stmt = select(Turn).where(Turn.room_id == room.id)
    # Sorted by seq, not created_at. Two turns created in the same transaction
    # share a created_at — Postgres' now() is the transaction start time — so
    # ordering by it would be arbitrary between them. seq is monotonic by
    # construction, and it is what ix_turns_room_id_seq indexes.
    stmt = apply_cursor(
        stmt,
        sort_column=Turn.seq,
        id_column=Turn.id,
        cursor=cursor,
        limit=limit,
    )
    return list((await db.execute(stmt)).scalars().all())


async def get_turn(db: AsyncSession, student: Student, turn_id: UUID) -> Turn:
    """One turn with its attempts, scoped to the caller.

    The join to rooms is what enforces ownership: a turn belonging to someone
    else's room is simply not found, exactly as for rooms themselves.
    """
    stmt = (
        select(Turn)
        .join(Room, Turn.room_id == Room.id)
        .where(Turn.id == turn_id, Room.student_id == student.id)
    )
    turn = (await db.execute(stmt)).scalar_one_or_none()
    if turn is None:
        raise NotFoundError(f"No turn with id {turn_id}.")
    await db.refresh(turn, attribute_names=["attempts"])
    return turn
