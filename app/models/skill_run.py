"""Skill runs, and the quizzes they produce.

A **tool** answers a question the model asked itself, mid-turn, in a few hundred
tokens. A **skill** is a whole piece of work a student asked for: build me a
quiz, solve this equation step by step. It has its own input, its own output
shape, its own checks, and it is worth keeping.

## Why a skill run hangs off a turn

Because everything the model does in this project is a turn, and a skill is no
exception. A student asking "quiz me on photosynthesis" has asked a question and
received an answer — the fact that the answer is five structured questions rather
than a paragraph does not make it a different kind of event.

Three things follow, and they are the reason this shape was chosen over a
free-standing table:

- **One audit trail.** The model calls a skill makes are `turn_attempts` rows with
  `purpose='skill'`, alongside the tutor's and the repair loop's. `GET
  /turns/{id}` therefore shows a skill run in exactly the detail the brief asks
  for — prompt, attempts, failures, tokens — with no second inspection format to
  learn.
- **One timeline.** A quiz built in a room appears in that room's turn list,
  where the student asked for it. A separate table would have left the
  conversation with a hole in it.
- **It counts as studying.** Study history reads succeeded, in-scope turns, so a
  quiz on photosynthesis contributes its concepts like any other turn.

## Why the quiz has tables and the solver has JSONB

The brief names four ways a quiz goes wrong — duplicate choices, the wrong count,
an invalid answer, predictable answer positions. Two of those are things a
database can make **impossible to store**, and the two unique indexes below do
exactly that. So the quiz gets real tables, because there are real constraints to
hang on them.

`step_solver` has no equivalent. No constraint can express "this algebra step
follows from the last one" — that is sympy's job, at write time, before the row
exists. Giving its steps three more tables would buy nothing a `JSONB` column
does not, so `skill_runs.result` holds them.

The asymmetry is the point: structure where the database can enforce something,
documents where it cannot.

## What is deliberately not here

**Attempts.** They are `turn_attempts` rows, not a column. `checks` stays because
it records something that table has no place for: the sympy verdict on each step
of a solution, which is about the finished result rather than about any one call.
"""

import enum
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)

# Aliased because `QuizChoice` has a column called `text`, which shadows the
# function inside the class body and turns `text(...)` into a confusing
# "MappedColumn object is not callable".
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, CreatedAtMixin, UUIDPrimaryKey

if TYPE_CHECKING:
    from app.models.turn import Turn

# Options per question. Fixed rather than configurable: four is what school
# multiple choice looks like, and every check below counts against it.
CHOICES_PER_QUESTION = 4


class SkillStatus(enum.StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class SkillRun(UUIDPrimaryKey, CreatedAtMixin, Base):
    __tablename__ = "skill_runs"

    turn_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("turns.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )

    skill_name: Mapped[str] = mapped_column(String(64), nullable=False)
    # Recorded per run, so a stored result always says which version of the skill
    # produced it — the same reason attempts record the prompt version.
    skill_version: Mapped[int] = mapped_column(Integer, nullable=False)

    status: Mapped[str] = mapped_column(String(16), nullable=False)

    # What the student asked for: the topic, the equation, the question count.
    request: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # The finished work, for skills whose output is not relational. Quizzes leave
    # this null and use the tables below.
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Every check that ran, and what it found. Stored rather than merely logged,
    # because "we validate the output" is a claim, and this is the evidence — it
    # is what the inspection endpoint shows.
    checks: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    failure_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    turn: Mapped["Turn"] = relationship(back_populates="skill_run")
    questions: Mapped[list["QuizQuestion"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="QuizQuestion.position",
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('succeeded', 'failed')", name="status_is_valid"
        ),
        # A failure has to say why. The same rule as turns: a terminal state that
        # explains nothing is a row nobody can act on.
        CheckConstraint(
            "(status = 'succeeded' AND failure_reason IS NULL) OR "
            "(status = 'failed' AND failure_reason IS NOT NULL)",
            name="failures_explain_themselves",
        ),
    )


class QuizQuestion(UUIDPrimaryKey, Base):
    __tablename__ = "quiz_questions"

    run_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("skill_runs.id", ondelete="CASCADE"),
        nullable=False,
    )

    position: Mapped[int] = mapped_column(Integer, nullable=False)
    stem: Mapped[str] = mapped_column(Text, nullable=False)

    # Why the right answer is right. Shown after answering, which is the part
    # that makes a quiz teach rather than only test.
    explanation: Mapped[str] = mapped_column(Text, nullable=False)

    # Where this came from, when the room had materials to build it from:
    # "handout.pdf, page 4". Null when the quiz was built from general knowledge.
    source: Mapped[str | None] = mapped_column(String(300), nullable=True)

    run: Mapped["SkillRun"] = relationship(back_populates="questions")
    choices: Mapped[list["QuizChoice"]] = relationship(
        back_populates="question",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="QuizChoice.position",
    )

    __table_args__ = (
        UniqueConstraint("run_id", "position", name="uq_questions_run_position"),
        CheckConstraint("position >= 0", name="position_is_not_negative"),
    )


class QuizChoice(UUIDPrimaryKey, Base):
    """One option. Two of the brief's four quiz failures die here.

    The brief lists what a bad quiz looks like: *"duplicate choices, the wrong
    count, an invalid answer, or predictable answer option patterns"*. Our Python
    checks catch all four and ask the model to fix them. The two indexes below are
    the safety net for when a future code path forgets — they do not report a
    duplicate option, they make one **unstorable**.

    The other two cannot be constraints, and it is worth being precise about why:

    - *wrong count* is a fact about a set of rows, not about any one of them. It
      needs a trigger, and a trigger is a piece of program logic hidden inside the
      database where nobody reads it. Enforced in code instead.
    - *predictable positions* is a statistical property of many quizzes. No single
      row is wrong. It is fixed by shuffling in code, with a seeded generator.
    """

    __tablename__ = "quiz_choices"

    question_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("quiz_questions.id", ondelete="CASCADE"),
        nullable=False,
    )

    position: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    is_correct: Mapped[bool] = mapped_column(Boolean, nullable=False)

    question: Mapped["QuizQuestion"] = relationship(back_populates="choices")

    __table_args__ = (
        UniqueConstraint("question_id", "position", name="uq_choices_question_position"),
        CheckConstraint("position >= 0", name="position_is_not_negative"),
        # Two options in the same question cannot say the same thing. lower() and
        # btrim() mean "Paris", "paris " and "PARIS" are one answer, which is how
        # a student reads them.
        Index(
            "ux_choice_text",
            "question_id",
            sql_text("lower(btrim(text))"),
            unique=True,
        ),
        # A question can have exactly one correct option. The WHERE clause is what
        # makes this work: without it, every question could hold only one *wrong*
        # option too.
        Index(
            "ux_choice_correct",
            "question_id",
            unique=True,
            postgresql_where=sql_text("is_correct"),
        ),
    )
