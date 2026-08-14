"""What a skill is, and the loop every skill shares.

The brief sets the bar: skills must *"provide real student value, extend the
LLM's capabilities, and not be renamed versions of the same prompt."*

That last clause is the whole design constraint, and this file is the answer to
it. A skill here is **a prompt plus a function that checks the model's work**:

    generate(...)  ->  ask the model
                   ->  run the skill's own checks, in Python
                   ->  problems? tell the model exactly what was wrong, ask again
                   ->  still broken? fail loudly rather than serve it

Take the checks away and each skill really would be a renamed prompt. With them,
`step_solver` produces algebra that a computer algebra system has agreed with, and
`quiz_builder` produces a quiz whose options are known to be distinct and whose
correct answers are known not to sit in the same slot. Neither property can be
obtained by asking more nicely.

## Why this is not the tutor's repair loop

`services/turn.py` has one too, and they look similar. They are not the same
thing: the tutor's loop wraps a conversation with tools, budgets and a deadline,
and its checks are about *any* answer being safe to show. A skill's checks are
specific to one output shape. Sharing the code would mean one function serving two
sets of reasons, and the first change to either would fork it anyway.

What the two loops **do** share is where they write. Every call below records a
`turn_attempts` row with `purpose='skill'`, the same table and the same columns
the tutor uses. So one endpoint — `GET /turns/{id}` — answers the brief's
"prompt, model attempts, failures, token usage" for a quiz exactly as it does for
a conversation, and there is no second inspection format to learn.
"""

import json
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import ClassVar

from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.llm import LLMClient, LLMError, LLMRequest
from app.llm.retry import RETRIABLE, backoff
from app.models.room import Room
from app.models.student import Student
from app.models.turn import Turn
from app.models.turn_attempt import AttemptPurpose, TurnAttempt
from app.prompts.loader import load_prompt
from app.reliability.failures import ResponseInvalid
from app.reliability.parsing import extract_json

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SkillContext:
    """Everything a skill may know.

    Same rule as `ToolContext`: the student and room are resolved from the
    request before anything ran, so no skill takes an identity argument and none
    can reach another student's data.
    """

    db: AsyncSession
    student: Student
    room: Room
    llm: LLMClient
    # The turn this run belongs to. Every model call a skill makes is recorded
    # against it, so a skill run is inspectable through the same endpoint as a
    # conversation rather than through a shape of its own.
    turn: Turn


class SkillFailure(Exception):
    """A skill that could not produce something worth showing a student.

    `reason` is a stable code for clients; `detail` is for humans. Failing is a
    real outcome here — a quiz with two identical options is worse than no quiz,
    so the loop stops rather than degrading.
    """

    def __init__(
        self,
        reason: str,
        detail: str,
        *,
        checks: list[dict] | None = None,
    ) -> None:
        self.reason = reason
        self.detail = detail
        # Carried on the exception so the verdicts reached before giving up are
        # saved rather than thrown away. The prompts and replies themselves are
        # already `turn_attempts` rows by this point — they were written as each
        # call happened, so a failure loses nothing.
        self.checks = checks or []
        super().__init__(detail)


@dataclass
class Generation:
    """One finished `generate` call: the payload, and the checks that cleared it.

    No `attempts` field — those are rows in `turn_attempts`, written as they
    happen. `checks` records what the skill's own code concluded, which that
    table has no column for.
    """

    payload: BaseModel
    checks: list[dict] = field(default_factory=list)


class Skill(ABC):
    """One thing a student can ask for, end to end."""

    # Stored on every run, so a result always says what produced it.
    name: ClassVar[str]
    version: ClassVar[int]
    # What the student may ask for, as a Pydantic model.
    args_model: ClassVar[type[BaseModel]]

    @abstractmethod
    async def run(self, args: BaseModel, context: SkillContext) -> Generation:
        """Do the work, or raise `SkillFailure`."""

    @abstractmethod
    def describe_request(self, args: BaseModel) -> str:
        """What the student asked for, as a sentence.

        Becomes the turn's `student_message`, so it is what the room's timeline
        shows and what the tutor sees as context on the next question. A skill
        invocation is not typed as prose, so the prose is written here rather
        than left as an empty message or a JSON blob.
        """

    @abstractmethod
    def summarise(self, args: BaseModel, payload: BaseModel) -> str:
        """One line describing what was produced.

        Becomes the turn's `answer_text`. `terminal_states_are_complete` requires
        a succeeded turn to have one, and that constraint is right: a timeline
        entry with no answer is a gap. The full result lives in the run.
        """

    def concepts(self, args: BaseModel, payload: BaseModel) -> list[str]:
        """Topic tags for study history. Empty when the skill cannot name one.

        Default rather than abstract because guessing is worse than abstaining —
        `step_solver` knows it solved an equation but not whether that was
        "linear equations" or "rearranging formulae", and inventing a tag would
        put a wrong entry in the student's own record of what they studied.
        """
        return []


async def generate(
    context: SkillContext,
    *,
    prompt_name: str,
    prompt_version: int,
    variables: dict,
    schema: type[BaseModel],
    check: Callable[[BaseModel], list[str]],
    repair_prompt: str,
) -> Generation:
    """Ask the model, check the answer in code, and ask again if it is wrong.

    `check` returns a list of problems written **for the model to read**, because
    they are pasted straight into the retry. "question 3 has two options reading
    'photosynthesis'" tells it what to change; "ValidationError" does not.

    Three budgets, kept apart because they are three different problems. A
    **network retry** re-sends the same prompt after a provider error — nothing
    about it was wrong. A **repair** sends a different prompt because the answer
    was unusable. The **deadline** bounds the sum. Collapsing the first two into
    one counter would mean three rate limits ate the repair budget, and the model
    would never get its second chance at the thing it actually got wrong.

    Both counters are the tutor's settings, not new ones: the same reasoning
    applies and a second pair of knobs would be a second pair to tune.

    Two things bound the wall clock, and they bound different failures. Each call
    carries `llm_request_timeout_seconds`, set on the provider client, which is
    the only thing that can end a request that hangs. `skill_deadline_seconds` is
    checked between attempts, which is what stops several slow-but-successful
    calls adding up past what anyone will wait for.
    """
    settings = get_settings()
    prompt = load_prompt(prompt_name, prompt_version)
    base_prompt = prompt.render(**variables)
    rendered = base_prompt

    deadline = time.monotonic() + settings.skill_deadline_seconds
    checks: list[dict] = []
    problems: list[str] = []
    retries_left = settings.max_network_retries
    repairs_left = settings.max_repair_attempts
    retry_number = 0
    repairing = False

    # A bounded loop rather than `while True`, and the same backstop the tutor
    # uses. Both budgets above bite long before this ceiling; the bound is
    # structural, so the loop still terminates if either is later raised without
    # anyone rechecking the sum.
    for attempt_no in range(1, settings.max_attempts_per_turn + 1):
        if time.monotonic() > deadline:
            raise SkillFailure(
                "DEADLINE_EXCEEDED",
                f"Gave up after {settings.skill_deadline_seconds}s. The checks had "
                f"not passed by then: " + "; ".join(problems),
                checks=checks,
            )

        request = LLMRequest(
            prompt=rendered,
            model=settings.gemini_model,
            # Lower on a repair: that call is not being asked to be creative, it
            # is being asked to comply. Same reasoning as prompts/tutor_repair.
            temperature=0.1 if repairing else prompt.temperature,
            response_schema=schema,
        )
        attempt = TurnAttempt(
            turn_id=context.turn.id,
            attempt_no=attempt_no,
            purpose=AttemptPurpose.SKILL.value,
            prompt_name=prompt_name,
            prompt_version=prompt_version,
            prompt_sha256=prompt.sha256,
            rendered_prompt=rendered,
            request_params=request.as_params(),
            started_at=datetime.now(UTC),
        )
        context.db.add(attempt)
        # Written before the call, exactly as the tutor loop does it, so a crash
        # mid-request still leaves evidence that the call was made and what was
        # sent.
        await context.db.flush()

        try:
            response = await context.llm.generate(request)
        except LLMError as exc:
            # Recorded before deciding whether to retry, so a retried call keeps
            # its own row and the turn reports what it really cost.
            attempt.finished_at = datetime.now(UTC)
            attempt.error_type = exc.error_type
            attempt.error_detail = exc.detail

            if isinstance(exc, RETRIABLE) and retries_left > 0:
                retries_left -= 1
                await backoff(settings, retry_number)
                retry_number += 1
                continue  # same prompt, same purpose — nothing about it was wrong

            # The failure keeps the provider's own name for what went wrong.
            # `TurnFailureReason` carries the same three values as
            # `LLMError.error_type`, so a skill turn now reports the cause its
            # attempt row recorded; collapsing everything to PROVIDER_UNAVAILABLE
            # meant the turn and the attempt under it disagreed in one response.
            raise SkillFailure(
                exc.error_type,
                f"The model call did not produce usable text — {exc}",
                checks=checks,
            ) from exc

        attempt.finished_at = datetime.now(UTC)
        attempt.raw_response = response.text
        attempt.finish_reason = response.finish_reason
        attempt.prompt_tokens = response.prompt_tokens
        attempt.output_tokens = response.output_tokens
        attempt.thought_tokens = response.thought_tokens

        # `response_schema` makes malformed JSON unlikely, not impossible — and a
        # truncated reply is still cut off mid-object. S3's parser handles both,
        # so the two layers it owns are reused here rather than rewritten.
        payload: BaseModel | None = None
        try:
            payload = schema.model_validate(extract_json(response.text))
        except ResponseInvalid as exc:
            problems = [failure.detail for failure in exc.failures]
        except ValidationError as exc:
            problems = [
                f"{'.'.join(str(part) for part in error['loc']) or 'response'}: "
                f"{error['msg']}"
                for error in exc.errors()
            ]
        else:
            problems = check(payload)

        # The same column the tutor's content checks write to, so one reader
        # covers both: "which layer objected, and what did it say".
        attempt.validation_failures = problems or None
        checks.append({"attempt_no": attempt_no, "problems": problems})

        if payload is not None and not problems:
            logger.info(
                "skill %s produced a valid result on attempt %d", prompt_name, attempt_no
            )
            return Generation(payload=payload, checks=checks)

        logger.info(
            "skill %s attempt %d failed its checks: %s",
            prompt_name,
            attempt_no,
            json.dumps(problems)[:400],
        )

        if repairs_left <= 0:
            break
        repairs_left -= 1
        repairing = True
        # Built from the original each time, never appended to the last attempt's
        # prompt. Appending compounded: the third call carried the second call's
        # repair block *and* the first call's problems, which by then described a
        # draft that no longer existed.
        #
        # Still short of what the tutor does. `prompts/tutor_repair/v1.md` is a
        # separate versioned file that shows the model its own rejected output;
        # this pastes an instruction from Python onto the original prompt, so the
        # model is told what to fix without being shown the thing to fix, and
        # `prompt_sha256` on these rows describes only the base file. Both are
        # written up in the README as the next change here.
        rendered = f"{base_prompt}\n\n{repair_prompt}\n" + "\n".join(
            f"- {problem}" for problem in problems
        )

    raise SkillFailure(
        "CHECKS_FAILED",
        "The model could not produce a result that passed our checks after "
        f"{len(checks)} attempts. Last problems: " + "; ".join(problems),
        checks=checks,
    )
