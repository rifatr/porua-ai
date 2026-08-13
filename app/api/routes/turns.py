"""Turn endpoints.

A turn is one question and answer inside a room. This is the only place in the
API that calls Gemini.
"""

from uuid import UUID

from fastapi import APIRouter, Query, status

from app.api.deps import LLM, CurrentStudent, DbSession, PageLimit
from app.api.pagination import Page, build_page
from app.schemas.turn import TurnCreate, TurnDetail, TurnRead
from app.services import turn as turn_service

rooms_router = APIRouter(prefix="/rooms", tags=["turns"])
turns_router = APIRouter(prefix="/turns", tags=["turns"])


@rooms_router.post(
    "/{room_id}/turns",
    response_model=TurnRead,
    status_code=status.HTTP_201_CREATED,
    summary="Ask the tutor a question",
    description=(
        "Runs one turn: builds the prompt from the room's recent history, calls "
        "Gemini, and saves the result.\n\n"
        "This is the slow, billable endpoint — expect seconds, not milliseconds. "
        "Every call made while producing the turn is recorded, including calls "
        "that failed; see `GET /turns/{turn_id}`.\n\n"
        "A turn that fails returns `status: \"failed\"` with a typed "
        "`failure_reason` and **no answer**. It never returns a partial or "
        "unchecked answer."
    ),
)
async def create_turn(
    room_id: UUID,
    data: TurnCreate,
    db: DbSession,
    student: CurrentStudent,
    llm: LLM,
) -> TurnRead:
    turn = await turn_service.create_turn(db, student, room_id, data.message, llm)
    return TurnRead.model_validate(turn)


@rooms_router.get(
    "/{room_id}/turns",
    response_model=Page[TurnRead],
    summary="What happened in this room",
    description=(
        "The room's conversation, newest first. Cursor-paged.\n\n"
        "This is the conversation view: it does not include prompts or raw model "
        "responses, which can be tens of kilobytes each. Use "
        "`GET /turns/{turn_id}` for those."
    ),
)
async def list_turns(
    room_id: UUID,
    db: DbSession,
    student: CurrentStudent,
    limit: PageLimit,
    cursor: str | None = Query(
        default=None,
        description="The `next_cursor` value from the previous response.",
    ),
) -> Page[TurnRead]:
    rows = await turn_service.list_turns(
        db, student, room_id, cursor=cursor, limit=limit
    )
    return build_page(rows, limit=limit, sort_attr="seq", serializer=TurnRead)


@turns_router.get(
    "/{turn_id}",
    response_model=TurnDetail,
    summary="Inspect one turn in full",
    description=(
        "Everything that happened while producing this turn:\n\n"
        "- **the prompt** actually sent, plus the name, version and checksum of "
        "the prompt file it came from\n"
        "- **every model attempt**, including ones that failed, with the raw "
        "response and the error\n"
        "- **token usage**, with thinking tokens counted separately — "
        "`gemini-2.5-flash` reasons before answering, and those tokens are billed "
        "but never appear in the answer\n"
        "- **the final saved result**\n\n"
        "Tool activity and validation failures appear here from slices S3 and S4."
    ),
)
async def get_turn(
    turn_id: UUID,
    db: DbSession,
    student: CurrentStudent,
) -> TurnDetail:
    turn = await turn_service.get_turn(db, student, turn_id)
    return TurnDetail.from_model(turn)
