"""Running a turn.

One loop, one row per model call. Everything the brief asks for under "assume the
model will sometimes ignore instructions, return malformed data, or produce
structurally valid but poor content" happens inside `create_turn` below.

## Why it is one loop and not two

There are two ways a call can go wrong, and it is tempting to handle them
separately: wrap the client in something that retries network errors, and put the
validation and repair around the outside. That was the first design and it was
wrong, for one reason — a retry hidden inside a wrapper writes no row. A turn that
took four seconds because it hit two rate limits would look identical to one that
was simply slow, and the endpoint the brief asks for ("inspect a turn in enough
detail to understand its prompt, model attempts, failures") would be quietly
lying. So both budgets are spent in the same loop, and every iteration writes a
`turn_attempts` row whatever happens to it.

## The two budgets

**Network retries** re-send the *same* prompt after a transient provider error.
**Repairs** send a *different* prompt because the response came back unusable.
They are counted separately because three rate limits and three bad answers are
different problems, and one counter would hide which one you had.

Both are per turn rather than per prompt: the student is waiting on the turn, not
on any individual call, so the cost that matters is the total.

## What is not retried

`ResponseBlocked` — a safety refusal. Retrying nondeterministic failures is
sensible; retrying a decision is not. The same prompt will be refused again, so
the only thing a retry buys is a slower failure.

## Tools, and the four budgets

From S4 the same loop also runs tools. A response either asks for tools or answers;
if it asks, we run them, append the results, and go round again without consuming a
repair — nothing was *wrong* with that response, it simply was not an answer yet.

Four budgets bound it, and they are separate because they fail for different
reasons:

| Budget | Counts | Default |
|---|---|---|
| `max_agent_iterations` | rounds of ask-run-ask | 5 |
| `max_tool_calls_per_turn` | individual tool runs | 6 |
| `max_network_retries` | provider errors | 2 |
| `max_repair_attempts` | unusable answers | 2 |

Over all of them sits `turn_deadline_seconds`, checked at the top of every
iteration, because a student waiting synchronously would rather have an error at
45 seconds than an answer at three minutes.

When the tool budgets run out the tools stop being *offered* rather than the turn
failing. The model then has to answer with what it already gathered, which is
almost always possible and is a far better outcome than an error.
"""

import asyncio
import json
import logging
import random
import time
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.errors import NotFoundError
from app.api.pagination import apply_cursor
from app.config import Settings, get_settings
from app.llm import EmptyResponse, LLMClient, LLMError, LLMRequest, ProviderUnavailable
from app.llm.base import ToolInvocation, ToolResult
from app.models.room import Room
from app.models.student import Student
from app.models.tool_call import ToolCall
from app.models.turn import Turn, TurnFailureReason, TurnStatus
from app.models.turn_attempt import AttemptPurpose, TurnAttempt
from app.prompts.loader import Prompt, load_prompt
from app.reliability.failures import ResponseInvalid
from app.reliability.pipeline import validate_tutor_response
from app.schemas.tutor import TutorAnswer
from app.services import room as room_service
from app.tools import registry as tool_registry
from app.tools.base import ToolContext, ToolFailure, ToolStatus
from app.tools.registry import InvalidArguments

logger = logging.getLogger(__name__)

TUTOR_PROMPT = "tutor_system"
TUTOR_PROMPT_VERSION = 3
REPAIR_PROMPT = "tutor_repair"
REPAIR_PROMPT_VERSION = 1

# Worth retrying because it may come out differently next time. A rate limit
# passes; an empty response usually means gemini-2.5-flash spent its whole output
# budget thinking, and how much it thinks varies run to run at temperature > 0.
RETRIABLE = (ProviderUnavailable, EmptyResponse)

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
    """The last few completed, on-topic turns, oldest first for the prompt.

    Off-topic turns are left out. If a student asks about football in an algebra
    room, the tutor declines — and replaying that exchange into the next prompt
    would spend tokens teaching the model to talk about football, in a room whose
    entire purpose is not to. Filtering it here is why `in_scope` is stored rather
    than merely observed.
    """
    stmt = (
        select(Turn)
        .where(
            Turn.room_id == room.id,
            Turn.status == TurnStatus.SUCCEEDED.value,
            Turn.in_scope.is_(True),
        )
        .order_by(Turn.seq.desc())
        .limit(CONTEXT_TURN_COUNT)
    )
    rows = list((await db.execute(stmt)).scalars().all())
    return list(reversed(rows))


async def _backoff(settings: Settings, retry_number: int) -> None:
    """Wait before re-sending, longer each time, with jitter.

    Growing the gap gives an overloaded provider room to recover instead of being
    hit again immediately. The random part matters when several requests fail at
    once: without it they would all wake at the same instant and rate-limit each
    other again, which is the thundering-herd problem.
    """
    base = settings.retry_base_delay_seconds
    if base <= 0:  # tests set this to 0 so the suite does not sleep
        return
    delay = base * (2**retry_number) * (0.5 + random.random())
    logger.info("backing off %.2fs before retry %d", delay, retry_number)
    await asyncio.sleep(delay)


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


async def _fail(db: AsyncSession, turn: Turn, reason: TurnFailureReason) -> Turn:
    """End the turn with no answer. There is no partial success here — a turn
    either carries something that passed every check, or it carries nothing and
    says why."""
    _finish_turn(turn, failure_reason=reason.value)
    await db.flush()
    await db.refresh(turn)
    logger.warning("turn %s failed: %s", turn.id, reason.value)
    return turn


def _cache_key(invocation: ToolInvocation) -> str:
    """Identity of a tool call: its name and its arguments, order-insensitive.

    `sort_keys` matters — the model can emit the same two arguments in either
    order, and without it the cache would miss and the repeat would be paid for.
    """
    return (
        f"{invocation.name}"
        f"({json.dumps(invocation.arguments, sort_keys=True, default=str)})"
    )


async def _run_tool(
    db: AsyncSession,
    attempt: TurnAttempt,
    call_no: int,
    invocation: ToolInvocation,
    context: ToolContext,
    cache: dict[str, dict],
    settings: Settings,
    *,
    over_budget: bool,
) -> tuple[ToolResult, bool]:
    """Run one requested tool and record it, whatever happens.

    Returns what to send back to the model, and whether the turn must stop.

    Every branch below writes a `tool_calls` row, including the branches where
    nothing runs. A request for a tool that does not exist is exactly the evidence
    that the fixed registry works, and it would be invisible if only successful
    calls were stored.

    A result always comes back, even for a rejected call. Function calling is a
    conversation: leaving a call unanswered makes the next request malformed, so
    "no" has to be said out loud rather than by silence.
    """
    row = ToolCall(
        attempt_id=attempt.id,
        call_no=call_no,
        tool_name=invocation.name,
        # Stored before validation, so a rejected call still shows what was asked.
        arguments=invocation.arguments,
        started_at=datetime.now(UTC),
        status=ToolStatus.OK.value,
    )

    def finish(
        status: ToolStatus, *, result: dict | None = None, error: str | None = None
    ) -> ToolResult:
        """Complete the row, then add it. Both halves matter.

        The row is added here rather than up front because a tool may query the
        database, and SQLAlchemy autoflushes pending objects before a query. An
        unfinished row flushed mid-call has status `ok` and no result yet, which
        violates `tool_calls_explain_themselves` — the constraint found this the
        first time `query_study_history` ran. Adding a complete row instead means
        there is never a half-written one to flush.

        This is the opposite of `turn_attempts`, which *is* written before its
        call. The difference is what a crash would cost: an attempt holds the
        prompt, which is unrecoverable, while everything about a tool call is
        already known from the attempt that requested it.
        """
        row.status = status.value
        row.result = result
        row.error_detail = error
        row.finished_at = datetime.now(UTC)
        db.add(row)
        return ToolResult(name=invocation.name, content=result or {"error": error})

    # Protection 4b: the per-turn budget. Checked before anything else, because a
    # spent budget means nothing runs regardless of what was asked for.
    if over_budget:
        return (
            finish(
                ToolStatus.FAILED,
                error=(
                    f"Tool budget spent: {settings.max_tool_calls_per_turn} calls "
                    f"is the limit for one turn. Answer with what you already have."
                ),
            ),
            False,
        )

    # Protection 5: the same call twice. The limits would end a loop eventually;
    # this ends it now, and tells the model why so it stops rather than varying
    # the arguments slightly and trying again.
    key = _cache_key(invocation)
    if key in cache:
        repeated = cache[key] | {
            "note": (
                "You already called this with these arguments. This is the same "
                "result. Do not call it again — answer the student now."
            )
        }
        logger.info("tool %s repeated on turn attempt %s", invocation.name, attempt.id)
        return finish(ToolStatus.OK, result=repeated), False

    # Protection 1: the fixed registry. A miss returns None; there is no fallback
    # that could turn an arbitrary string into something callable.
    tool = tool_registry.resolve(invocation.name)
    if tool is None:
        return (
            finish(
                ToolStatus.UNKNOWN_TOOL,
                error=(
                    f"There is no tool called {invocation.name!r}. "
                    f"Available: {', '.join(tool_registry.names())}."
                ),
            ),
            False,
        )

    # Protection 2: arguments become a checked object or the tool never runs.
    try:
        args = tool_registry.validate_arguments(tool, invocation.arguments)
    except InvalidArguments as exc:
        return finish(ToolStatus.INVALID_ARGUMENTS, error=exc.detail), False

    # Protection 4a: the per-tool timeout.
    try:
        result = await asyncio.wait_for(
            tool.run(args, context), timeout=settings.tool_timeout_seconds
        )
    except TimeoutError:
        # The turn stops here. `wait_for` cancels the coroutine, and a database
        # query cancelled mid-flight leaves the session in a state we should not
        # keep writing through. Failing loudly beats a turn built on a connection
        # of unknown health.
        return (
            finish(
                ToolStatus.TIMED_OUT,
                error=f"{invocation.name} exceeded {settings.tool_timeout_seconds}s.",
            ),
            True,
        )
    except ToolFailure as exc:
        # The tool understood the request and refused it — a bad expression, an
        # impossible range. Not our bug, and the model can act on the reason.
        return finish(ToolStatus.FAILED, error=exc.detail), False
    except Exception as exc:  # noqa: BLE001 - a tool bug must not break the turn
        logger.exception("tool %s raised", invocation.name)
        return (
            finish(ToolStatus.FAILED, error=f"The tool failed: {type(exc).__name__}."),
            False,
        )

    cache[key] = result
    return finish(ToolStatus.OK, result=result), False


async def create_turn(
    db: AsyncSession,
    student: Student,
    room_id: UUID,
    message: str,
    llm: LLMClient,
) -> Turn:
    """Ask the tutor a question, check the reply, and record every step."""
    room = await room_service.get_room(db, student, room_id)
    settings = get_settings()
    # One clock over everything. Every other budget sits inside it, so a turn
    # cannot creep past the point where the student would rather have an error.
    deadline = time.monotonic() + settings.turn_deadline_seconds

    turn = Turn(
        room_id=room.id,
        seq=await _next_seq(db, room),
        student_message=message,
        status=TurnStatus.RUNNING.value,
    )
    db.add(turn)
    await db.flush()

    tutor = load_prompt(TUTOR_PROMPT, TUTOR_PROMPT_VERSION)
    repair = load_prompt(REPAIR_PROMPT, REPAIR_PROMPT_VERSION)

    # Protection 3: identity, fixed here, from the request. No tool takes a
    # student id, so the model has no way to reach another student's data.
    context = ToolContext(db=db, student=student, room=room)

    prompt: Prompt = tutor
    purpose = AttemptPurpose.TUTOR
    rendered = tutor.render(
        education_level=student.education_level,
        room_title=room.title,
        recent_turns=await _recent_turns(db, room),
        student_message=message,
    )

    exchanges: tuple[tuple[ToolInvocation, ToolResult], ...] = ()
    tool_cache: dict[str, dict] = {}

    retries_left = settings.max_network_retries
    repairs_left = settings.max_repair_attempts
    tool_calls_left = settings.max_tool_calls_per_turn
    rounds_left = settings.max_agent_iterations
    retry_number = 0
    call_no = 0

    # A bounded loop rather than `while True`. Every budget below bites long
    # before this ceiling, but the bound is structural, so the loop terminates
    # even if the budgets are later raised without anyone rechecking the sum.
    for attempt_no in range(1, settings.max_attempts_per_turn + 1):
        if time.monotonic() > deadline:
            return await _fail(db, turn, TurnFailureReason.TURN_DEADLINE_EXCEEDED)

        # Tools are offered only while gathering, and only while both budgets
        # hold. When they run out the tools simply stop being offered, so the
        # model has to answer with what it has — the turn narrows rather than
        # failing, which is a better outcome for a student who is waiting.
        offer_tools = (
            purpose is AttemptPurpose.TUTOR and rounds_left > 0 and tool_calls_left > 0
        )

        request = LLMRequest(
            prompt=rendered,
            model=settings.gemini_model,
            temperature=prompt.temperature,
            tools=tool_registry.specs() if offer_tools else (),
            tool_exchanges=exchanges,
            # Mutually exclusive with tools — the provider rejects both together
            # (400, "Function calling with a response mime type ... is
            # unsupported"). So constrained decoding is unavailable exactly while
            # the model can call tools, and the parse/schema/content layers are
            # the only thing standing between that output and the student. Far
            # from making the pipeline redundant, `response_schema` leaves it
            # carrying the whole load for most of a tool-using turn.
            response_schema=None if offer_tools else TutorAnswer,
        )
        attempt = TurnAttempt(
            turn_id=turn.id,
            attempt_no=attempt_no,
            purpose=purpose.value,
            prompt_name=prompt.name,
            prompt_version=prompt.version,
            prompt_sha256=prompt.sha256,
            rendered_prompt=rendered,
            request_params=request.as_params(),
            started_at=datetime.now(UTC),
        )
        db.add(attempt)
        # Written before the call, so a crash mid-request still leaves evidence
        # that the call was made and what was sent.
        await db.flush()

        # --- the call ---------------------------------------------------------
        try:
            response = await llm.generate(request)
        except LLMError as exc:
            _finish_attempt(attempt, error_type=exc.error_type, error_detail=exc.detail)
            if isinstance(exc, RETRIABLE) and retries_left > 0:
                retries_left -= 1
                await _backoff(settings, retry_number)
                retry_number += 1
                continue  # same prompt, same purpose — nothing about it was wrong
            return await _fail(db, turn, TurnFailureReason(exc.error_type))

        _finish_attempt(
            attempt,
            raw_response=response.raw,
            finish_reason=response.finish_reason,
            prompt_tokens=response.prompt_tokens,
            output_tokens=response.output_tokens,
            thought_tokens=response.thought_tokens,
        )

        # --- tools ------------------------------------------------------------
        if response.tool_calls:
            rounds_left -= 1

            for invocation in response.tool_calls:
                call_no += 1
                result, must_stop = await _run_tool(
                    db,
                    attempt,
                    call_no,
                    invocation,
                    context,
                    tool_cache,
                    settings,
                    over_budget=tool_calls_left <= 0,
                )
                # Appended even when the call was refused. The model has to see a
                # response for every request it made, or the next call is
                # malformed — and it needs to read *why* to stop asking.
                exchanges += ((invocation, result),)
                tool_calls_left -= 1

                if must_stop:
                    await db.flush()
                    return await _fail(db, turn, TurnFailureReason.TOOL_TIMED_OUT)

            await db.flush()
            # Round the loop with the results in hand. No repair is consumed —
            # nothing was wrong with the response, it just was not an answer yet.
            continue

        # --- the checks -------------------------------------------------------
        try:
            answer = validate_tutor_response(response.text)
        except ResponseInvalid as invalid:
            # Recorded on the attempt that produced them, not on the turn. This is
            # what makes GET /turns/{id} able to show a rejected answer beside the
            # reason it was rejected — rather than only the version that survived.
            attempt.validation_failures = [f.as_dict() for f in invalid.failures]
            logger.info(
                "turn %s attempt %d rejected at the %s layer: %s",
                turn.id,
                attempt_no,
                invalid.layer,
                ", ".join(f.code for f in invalid.failures),
            )

            if repairs_left <= 0:
                return await _fail(db, turn, TurnFailureReason.INVALID_AFTER_REPAIR)

            repairs_left -= 1
            prompt = repair
            purpose = AttemptPurpose.REPAIR
            rendered = repair.render(
                education_level=student.education_level,
                room_title=room.title,
                student_message=message,
                previous_output=response.text,
                failures=invalid.failures,
            )
            continue

        # --- accepted ---------------------------------------------------------
        turn.answer_text = answer.answer.strip()
        turn.in_scope = answer.in_scope
        turn.concepts = answer.concepts
        _finish_turn(turn)

        await room_service.touch_room(db, room)
        await db.flush()
        # created_at is filled in by Postgres, so it has to be read back before
        # duration_ms can be computed from it.
        await db.refresh(turn)
        return turn

    return await _fail(db, turn, TurnFailureReason.ATTEMPT_LIMIT_EXCEEDED)


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
        # Loaded up front, in two extra queries rather than one per attempt. Lazy
        # loading is not merely slow here — it raises under asyncio, because the
        # implicit IO happens outside an await.
        .options(selectinload(Turn.attempts).selectinload(TurnAttempt.tool_calls))
    )
    turn = (await db.execute(stmt)).scalar_one_or_none()
    if turn is None:
        raise NotFoundError(f"No turn with id {turn_id}.")
    return turn
