"""Skill endpoints.

A skill is invoked directly rather than offered to the tutor as a tool. The brief
lists tools and skills as separate things, and the shapes differ: a tool answers a
question the model asked itself mid-sentence, in a few hundred tokens; a skill is
a piece of work a student asked for, with its own arguments, its own output and a
row worth keeping.

## One endpoint per skill, not one endpoint with a name in the path

The first version took the skill name as a path parameter and the arguments as an
untyped `dict`. It cost exactly what you would expect: Swagger could not show that
`quiz_builder` takes `topic` and `question_count` while `step_solver` takes
`problem`, so the single example on the shared endpoint was wrong for whichever
skill you were not looking at — an example that misleads being worse than none.

Typed routes give each skill its own request schema, its own example and its own
description, all generated from the `args_model` the skill already declared. The
cost is a handler per skill instead of a line in a dispatch table, which is the
right way round: a skill is a feature, and features are worth their own
documentation. The registry still owns *which* skills exist, and the service still
resolves the name through it, so there is still no path from input to arbitrary
code.
"""

from uuid import UUID

from fastapi import APIRouter, Query, status

from app.api.deps import LLM, CurrentStudent, DbSession, PageLimit
from app.api.pagination import Page, build_page
from app.schemas.skill import SkillRunRead, SkillRunSummary
from app.services import skill as skill_service
from app.skills.quiz_builder import QuizArgs
from app.skills.step_solver import SolveArgs

rooms_router = APIRouter(prefix="/rooms", tags=["skills"])
turns_router = APIRouter(prefix="/turns", tags=["skills"])

# Repeated on both POST routes rather than written twice in prose.
_SHARED = (
    "**A run is a turn.** It appears in the room's timeline where the student "
    "asked for it, and every model call it made is a `turn_attempts` row — so "
    "`GET /turns/{turn_id}` inspects it in the same detail as a conversation, "
    "with no second format to learn.\n\n"
    "A run is saved either way. `status` is `succeeded` or `failed`, and a failed "
    "run keeps the checks that rejected each attempt — output we refused to show "
    "is more informative than output we quietly patched."
)


@rooms_router.post(
    "/{room_id}/skills/quiz_builder/runs",
    response_model=SkillRunRead,
    status_code=status.HTTP_201_CREATED,
    summary="Build a quiz",
    description=(
        "A multiple-choice quiz, checked in code before it is stored: no "
        "duplicate options, exactly one correct answer, the right number of "
        "questions, and the answer position shuffled so it is not always the "
        "same slot.\n\n"
        "If the room has uploaded material the quiz is built from it and each "
        "question cites the page it came from.\n\n"
        "Fails with `CHECKS_FAILED` rather than serving a quiz that could not be "
        "made to pass — a quiz with two identical options is worse than none.\n\n"
        + _SHARED
    ),
)
async def build_quiz(
    room_id: UUID,
    args: QuizArgs,
    db: DbSession,
    student: CurrentStudent,
    llm: LLM,
) -> SkillRunRead:
    run = await skill_service.run_skill(
        db, student, llm, room_id, "quiz_builder", args.model_dump()
    )
    return SkillRunRead.model_validate(run)


@rooms_router.post(
    "/{room_id}/skills/step_solver/runs",
    response_model=SkillRunRead,
    status_code=status.HTTP_201_CREATED,
    summary="Solve an equation, step by step",
    description=(
        "Working for one equation, with **every step checked by a computer "
        "algebra system** before the student sees it. A step is accepted only if "
        "it has the same solutions as the step before it, which catches an "
        "arithmetic slip without needing to know what operation was claimed.\n\n"
        "Declines with `UNCHECKABLE_PROBLEM` for anything it cannot verify — word "
        "problems, several unknowns — because without its checks this is a "
        "renamed prompt, and the tutor endpoint already answers those well.\n\n"
        "The checks on the run record each step and whether sympy agreed, which "
        "is how you can tell verification actually ran.\n\n" + _SHARED
    ),
)
async def solve_step_by_step(
    room_id: UUID,
    args: SolveArgs,
    db: DbSession,
    student: CurrentStudent,
    llm: LLM,
) -> SkillRunRead:
    run = await skill_service.run_skill(
        db, student, llm, room_id, "step_solver", args.model_dump()
    )
    return SkillRunRead.model_validate(run)


@rooms_router.get(
    "/{room_id}/skills/runs",
    response_model=Page[SkillRunSummary],
    summary="Skill runs in this room",
    description=(
        "Newest first, cursor-paged. Summaries only — a run's questions, options "
        "and checks are the bulk of it, and a list is for choosing which one to "
        "open. Follow `turn_id` to `GET /turns/{turn_id}/skill-run`."
    ),
)
async def list_runs(
    room_id: UUID,
    db: DbSession,
    student: CurrentStudent,
    limit: PageLimit,
    cursor: str | None = Query(
        default=None,
        description="The `next_cursor` value from the previous response.",
    ),
) -> Page[SkillRunSummary]:
    rows = await skill_service.list_runs(
        db, student, room_id, cursor=cursor, limit=limit
    )
    return build_page(rows, limit=limit, sort_attr="seq", serializer=SkillRunSummary)


@turns_router.get(
    "/{turn_id}/skill-run",
    response_model=SkillRunRead,
    summary="The quiz or solution this turn produced",
    description=(
        "The arguments, every check and what it found, and the finished work. For "
        "`step_solver` the checks include each step and whether sympy agreed with "
        "it — which is how you can tell verification actually ran.\n\n"
        "The prompts, raw replies and token counts are on the turn: follow "
        "`turn_id` to `GET /turns/{id}`."
    ),
)
async def get_run(
    turn_id: UUID, db: DbSession, student: CurrentStudent
) -> SkillRunRead:
    run = await skill_service.get_run(db, student, turn_id)
    return SkillRunRead.model_validate(run)
