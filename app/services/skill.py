"""Running a skill, and saving what it produced.

## A skill run is a turn

Asking for a quiz is asking the room a question. So a run creates a `Turn`, and
everything follows from that: the model calls become `turn_attempts` rows with
`purpose='skill'`, the quiz appears in the room's timeline where the student
asked for it, `GET /turns/{id}` inspects it in the same detail as a conversation,
and study history counts it as work done — a quiz on photosynthesis contributes
its concepts like any other turn.

The alternative — a free-standing `skill_runs` table hanging off the room — was
built first and removed. It left the project with two audit trails of different
shapes, so "inspect the model attempts" meant one thing for a chat and another
for a quiz, and the room's conversation had a hole in it where the quiz was.

## The turn still has to be a complete turn

`terminal_states_are_complete` requires a succeeded turn to carry `answer_text`
and `in_scope`. That is not a constraint to work around — it is the reason each
skill returns a one-line summary of what it made. "Here is a 5-question quiz on
photosynthesis" is what belongs in a timeline; the quiz itself is a click away.

## Why a failed run is still a row

`status='failed'` with a reason, on both the turn and the run. A quiz that could
not be built after three tries is the most informative record in the table: the
attempts hold every prompt sent and every reply, and `checks` holds exactly which
rule rejected each one. That is the evidence for "we do not show broken output" —
deleting it would leave only the claim.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.errors import NotFoundError, ValidationFailedError
from app.api.pagination import apply_cursor
from app.llm import LLMClient
from app.models.room import Room
from app.models.skill_run import QuizChoice, QuizQuestion, SkillRun, SkillStatus
from app.models.student import Student
from app.models.turn import Turn, TurnFailureReason, TurnStatus
from app.services import room as room_service
from app.services import turn as turn_service
from app.skills import registry as skill_registry
from app.skills.base import Skill, SkillContext, SkillFailure
from app.skills.quiz_builder import DraftQuiz

logger = logging.getLogger(__name__)

# Which turn failure a skill failure maps to. The turn vocabulary is what a
# client already branches on, so a skill reuses it rather than inventing a
# parallel one; the precise skill reason stays on `skill_runs.failure_reason`.
#
# Only the skill-specific reasons are listed. The provider ones already share
# their names with `TurnFailureReason` — its docstring says those three mirror
# `LLMError.error_type` exactly — so they translate by name in `_turn_failure`.
_TURN_FAILURE = {
    "CHECKS_FAILED": TurnFailureReason.INVALID_AFTER_REPAIR,
    "DEADLINE_EXCEEDED": TurnFailureReason.TURN_DEADLINE_EXCEEDED,
    # Refused before any model call, so nothing was attempted and nothing is
    # wrong with the model. The closest honest turn-level reason is still that we
    # would not stand behind an answer.
    "UNCHECKABLE_PROBLEM": TurnFailureReason.INVALID_AFTER_REPAIR,
}


def _turn_failure(reason: str) -> TurnFailureReason:
    """A skill failure, in the turn's vocabulary."""
    try:
        return TurnFailureReason(reason)
    except ValueError:
        return _TURN_FAILURE.get(reason, TurnFailureReason.INVALID_AFTER_REPAIR)


async def run_skill(
    db: AsyncSession,
    student: Student,
    llm: LLMClient,
    room_id: UUID,
    skill_name: str,
    payload: dict,
) -> SkillRun:
    """Run one skill in one room, and save the result either way."""
    room = await room_service.get_room(db, student, room_id)

    skill = skill_registry.resolve(skill_name)
    if skill is None:
        raise NotFoundError(
            f"No skill called {skill_name!r}. Available: "
            f"{', '.join(skill_registry.names())}."
        )

    # The same posture as tool arguments: the request becomes a checked object
    # before any skill code runs, using the model the skill declared. This is a
    # 422 rather than a failed run, because nothing was attempted.
    try:
        args = skill.args_model.model_validate(payload)
    except ValueError as exc:
        raise ValidationFailedError(f"Invalid arguments for {skill_name}: {exc}") from exc

    turn = Turn(
        room_id=room.id,
        seq=await turn_service.next_seq(db, room),
        student_message=skill.describe_request(args),
        status=TurnStatus.RUNNING.value,
    )
    db.add(turn)
    await db.flush()

    run = SkillRun(
        # `turn_id`, not `turn=turn`. Assigning the relationship on an object that
        # is not yet in the session leaves the cascade with nothing to follow, and
        # the skill's first attempt triggers an autoflush right afterwards —
        # SQLAlchemy warns and quietly drops the association. Adding the row now,
        # by id, means it is in the session before anything can flush.
        turn_id=turn.id,
        skill_name=skill.name,
        skill_version=skill.version,
        status=SkillStatus.SUCCEEDED.value,
        request=args.model_dump(mode="json"),
        # Populated in Python for every path, so the response serialiser never
        # meets an unloaded collection and lazy-loads it outside the greenlet.
        questions=[],
    )
    db.add(run)
    context = SkillContext(db=db, student=student, room=room, llm=llm, turn=turn)

    try:
        outcome = await skill.run(args, context)
    except SkillFailure as exc:
        return await _fail(db, turn, run, exc)

    run.checks = outcome.checks
    if isinstance(outcome.payload, DraftQuiz):
        # A quiz goes into tables, where the database enforces its rules.
        _attach_quiz(run, outcome.payload)
    else:
        run.result = outcome.payload.model_dump(mode="json")

    turn.status = TurnStatus.SUCCEEDED.value
    turn.answer_text = skill.summarise(args, outcome.payload)
    turn.in_scope = True
    turn.concepts = skill.concepts(args, outcome.payload)
    turn.completed_at = datetime.now(UTC)

    await room_service.touch_room(db, room)
    await db.flush()
    await db.refresh(run, attribute_names=["created_at"])
    logger.info("skill %s succeeded as turn %s", skill.name, turn.id)
    return run


async def _fail(
    db: AsyncSession, turn: Turn, run: SkillRun, exc: SkillFailure
) -> SkillRun:
    logger.info("skill %s failed on turn %s: %s", run.skill_name, turn.id, exc.reason)

    run.status = SkillStatus.FAILED.value
    run.failure_reason = exc.reason
    run.failure_detail = exc.detail
    run.checks = exc.checks or None

    turn.status = TurnStatus.FAILED.value
    turn.failure_reason = _turn_failure(exc.reason).value
    turn.completed_at = datetime.now(UTC)

    await db.flush()
    await db.refresh(run, attribute_names=["created_at"])
    return run


def _attach_quiz(run: SkillRun, quiz: DraftQuiz) -> None:
    run.questions = [
        QuizQuestion(
            position=index,
            stem=question.stem.strip(),
            explanation=question.explanation.strip(),
            source=question.source,
            choices=[
                QuizChoice(
                    position=slot,
                    text=choice.text.strip(),
                    is_correct=choice.is_correct,
                )
                for slot, choice in enumerate(question.choices)
            ],
        )
        for index, question in enumerate(quiz.questions)
    ]


async def get_run(db: AsyncSession, student: Student, turn_id: UUID) -> SkillRun:
    """One run, addressed by its turn, if that turn is this student's.

    Addressed by `turn_id` and not by the run's own primary key: the two are 1:1,
    and offering both ids only gave a caller a way to pick the wrong one. Two
    joins because a run reaches its room through its turn; someone else's run is a
    404 for the same reason someone else's room is.
    """
    stmt = (
        select(SkillRun)
        .join(Turn, Turn.id == SkillRun.turn_id)
        .join(Room, Room.id == Turn.room_id)
        .where(SkillRun.turn_id == turn_id, Room.student_id == student.id)
        .options(selectinload(SkillRun.questions).selectinload(QuizQuestion.choices))
    )
    run = (await db.execute(stmt)).scalar_one_or_none()
    if run is None:
        raise NotFoundError(f"Turn {turn_id} has no skill run.")
    return run


@dataclass(frozen=True)
class RunSummary:
    """A list row. Deliberately not a `SkillRun`.

    The questions are never loaded here — `question_count` comes from a `COUNT`
    in the same query. Returning full runs made a one-quiz room a 10 KB response,
    and every question and option in it was read from the database to be thrown
    away by the serialiser.
    """

    turn_id: UUID
    skill_name: str
    skill_version: int
    status: str
    request: dict
    question_count: int
    failure_reason: str | None
    created_at: datetime
    # Sorted on, so the paging helper can turn the last row into a cursor.
    seq: int

    @property
    def id(self) -> UUID:
        """The tie-breaker `build_page` reads when it builds a cursor.

        Same value as `turn_id`, and the same column `apply_cursor` was given, so
        the two halves of paging agree. A run is addressed by its turn everywhere
        else; this is the one place the generic helper wants the name `id`.
        """
        return self.turn_id


async def list_runs(
    db: AsyncSession,
    student: Student,
    room_id: UUID,
    *,
    cursor: str | None,
    limit: int,
) -> list[RunSummary]:
    """Runs in this room, newest first, cursor-paged like every other list.

    Returns up to limit + 1 rows; the caller trims the extra one into the next
    cursor.
    """
    room = await room_service.get_room(db, student, room_id)

    questions = (
        select(func.count(QuizQuestion.id))
        .where(QuizQuestion.run_id == SkillRun.id)
        .scalar_subquery()
    )
    stmt = (
        select(
            SkillRun.turn_id,
            SkillRun.skill_name,
            SkillRun.skill_version,
            SkillRun.status,
            SkillRun.request,
            questions.label("question_count"),
            SkillRun.failure_reason,
            SkillRun.created_at,
            Turn.seq,
        )
        .join(Turn, Turn.id == SkillRun.turn_id)
        .where(Turn.room_id == room.id)
    )
    # Paged on the turn's `seq`, not on `created_at`: seq is unique per room and
    # strictly increasing, so it cannot tie the way two runs in the same second
    # can.
    stmt = apply_cursor(
        stmt,
        sort_column=Turn.seq,
        id_column=SkillRun.turn_id,
        cursor=cursor,
        limit=limit,
    )
    return [RunSummary(**row._mapping) for row in (await db.execute(stmt)).all()]


__all__ = ["Skill", "get_run", "list_runs", "run_skill"]
