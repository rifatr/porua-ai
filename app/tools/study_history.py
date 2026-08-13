"""The tutor asking what this student has been studying.

The brief's own example of a tool — *"past chat query tool"* — and the clearest
case for why tools exist at all. The model cannot know this. It is in our database,
it is different for every student, and it changes every day. No amount of prompting
produces it; the only honest options are to call a tool or to make something up.

## Why the argument is `days_back` and not two dates

A date range would be more expressive, and it is what the HTTP endpoint takes. But
the model does not reliably know today's date, so asking it for `from_date` invites
a confident, wrong answer — and a wrong date range produces an empty result that
looks like "you studied nothing", which is worse than an error.

`days_back` needs no knowledge of the calendar. "Last week" is 7, "last month" is
30, and the arithmetic happens here where today's date is a fact rather than a
guess. The endpoint keeps the full range for callers who do know what day it is.
"""

from datetime import UTC, datetime, timedelta
from typing import ClassVar

from pydantic import BaseModel, Field

from app.services import study_history as history_service
from app.tools.base import Tool, ToolContext

MAX_DAYS_BACK = 366


class StudyHistoryArgs(BaseModel):
    days_back: int = Field(
        default=30,
        ge=1,
        le=MAX_DAYS_BACK,
        description=(
            "How many days of history to look at, counting back from today. "
            "Use 7 for 'this week', 30 for 'this month', 365 for 'this year'."
        ),
        examples=[7, 30],
    )


class StudyHistoryTool(Tool):
    name = "query_study_history"
    description = (
        "Find out what this student has actually studied recently: which rooms "
        "they worked in, how many questions they asked, and which concepts came "
        "up most. Use it whenever the student asks about their own past — 'what "
        "did I study last week', 'have I covered this before', 'what should I "
        "revise' — and whenever knowing what they have already seen would change "
        "how you explain something. Only counts real study: failed and off-topic "
        "turns are excluded, and the busiest rooms and concepts are returned "
        "rather than every one. Never guess at this; the answer is in the "
        "database and guessing it wrong is worse than not answering."
    )
    args_model: ClassVar[type[BaseModel]] = StudyHistoryArgs

    async def run(self, args: StudyHistoryArgs, context: ToolContext) -> dict:
        today = datetime.now(UTC).date()

        # `context.student`, never an argument. The model has no way to name a
        # student, so it has no way to read anyone else's history.
        history = await history_service.get_study_history(
            context.db,
            context.student,
            from_date=today - timedelta(days=args.days_back),
            to_date=today,
        )

        return {
            "from_date": history.from_date.isoformat(),
            "to_date": history.to_date.isoformat(),
            "total_turns": history.total_turns,
            "rooms": [
                {
                    "title": room.title,
                    "turns": room.turns,
                    "last_studied": room.last_studied_at.date().isoformat(),
                }
                for room in history.rooms
            ],
            "concepts": [
                {"concept": item.concept, "turns": item.turns}
                for item in history.concepts
            ],
        }
