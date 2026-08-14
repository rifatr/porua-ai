"""The skill-run response shapes.

Two shapes, for the same reason turns have two: a list must stay small, and a
single run carries every question, every option and every check. One ten-question
quiz is about 10 KB — a room's worth of them in one unpaged response is not a list
endpoint, it is a download.

## There is one id here, and it is the turn's

A run is 1:1 with its turn (`UNIQUE(turn_id)`), so exposing a second identifier
bought nothing and cost real confusion: a response carrying both `id` and
`turn_id` invites reaching for `id`, and `GET /turns/{id}` then answers "no turn
with that id", which reads as the feature being broken. The run's primary key
stays internal, every path takes a turn id, and the mistake is no longer
available to make.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, computed_field


class QuizChoiceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    position: int
    text: str
    is_correct: bool

    @computed_field
    @property
    def label(self) -> str:
        """A, B, C, D — rendered rather than stored.

        The letter is a presentation detail of the position it already has;
        storing both would be two facts that can disagree.
        """
        return chr(ord("A") + self.position)


class QuizQuestionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    position: int
    stem: str
    explanation: str = Field(description="Why the right answer is right.")
    source: str | None = Field(
        description="The passage this question was built from, when the room had "
        "uploaded material. Null when it came from general knowledge."
    )
    choices: list[QuizChoiceRead]


class SkillRunSummary(BaseModel):
    """The list view: what was made, when, and whether it worked.

    No questions, no checks, no result — those are the bulk, and a list exists to
    choose which run to open.
    """

    model_config = ConfigDict(from_attributes=True)

    turn_id: UUID = Field(
        description="This run's id. Open it with `GET /turns/{turn_id}/skill-run`, "
        "or inspect the model calls behind it with `GET /turns/{turn_id}`."
    )
    skill_name: str
    skill_version: int
    status: str
    request: dict = Field(description="What the student asked for.")
    question_count: int = Field(
        description="Zero for a failed run, and for skills that produce no quiz."
    )
    failure_reason: str | None
    created_at: datetime


class SkillRunRead(BaseModel):
    """One run in full.

    `checks` is the point of this shape, not decoration. The brief asks for output
    you would be comfortable exposing through an API; this is what lets a caller
    see *why* it is safe — which rules ran and what they found. For `step_solver`
    it carries every step and whether a computer algebra system agreed with it.
    """

    model_config = ConfigDict(from_attributes=True)

    turn_id: UUID = Field(
        description="Inspect the model calls behind this run with "
        "`GET /turns/{turn_id}` — prompts, raw replies and token usage."
    )
    skill_name: str
    skill_version: int
    status: str
    request: dict
    result: dict | None = Field(
        description="For skills whose output is not relational — `step_solver`'s "
        "working. Null for a quiz, which lives in `questions`."
    )
    checks: list[Any] | None = Field(
        description="Every check that ran, per attempt. The prompts and raw "
        "replies are on the turn, not here."
    )
    failure_reason: str | None
    failure_detail: str | None
    questions: list[QuizQuestionRead]
    created_at: datetime
