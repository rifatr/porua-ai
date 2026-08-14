"""Skill: build a multiple-choice quiz the student can trust.

The brief uses a quiz as its worked example of unreliable model output:

> a quiz might contain **duplicate choices**, the **wrong count**, an **invalid
> answer**, or **predictable answer option patterns**

All four are handled here, and it is worth being precise about *where*, because
they do not all belong in the same place:

| Failure | Caught by | Why there |
|---|---|---|
| duplicate choices | code, then a unique index | code names it; the index bans it |
| wrong count | code | a fact about a row set, so it needs a trigger |
| invalid answer | code, then a partial index | same two reasons as duplicates |
| predictable positions | **code, by shuffling** | no one quiz is wrong; asking fails |

The last row is the interesting one. Models put the correct answer in the same
slot far more often than chance, and no instruction reliably stops it, because it
is not a rule the model is breaking — it is a bias in what it generates. So the
options are shuffled after the fact, by us, with a seeded generator. The property
is then true by construction rather than by request.

## Grounding

If the room has uploaded material, the quiz is built from it: the topic is
searched, the passages go into the prompt, and each question records which page it
came from. A quiz drawn from the student's own handout tests their course rather
than the subject in general — which is what they are marked on.
"""

import hashlib
import logging
import random
from typing import ClassVar

from pydantic import BaseModel, Field

from app.models.skill_run import CHOICES_PER_QUESTION
from app.reliability.checks import normalise
from app.services import document as document_service
from app.skills.base import Generation, Skill, SkillContext, generate

logger = logging.getLogger(__name__)

PROMPT_NAME = "quiz_builder"
PROMPT_VERSION = 1

MIN_QUESTIONS = 3
MAX_QUESTIONS = 10

REPAIR_INSTRUCTION = (
    "Your previous quiz was rejected by automatic checks. Fix exactly these "
    "problems and reply with the whole quiz again, in the same JSON shape:"
)


class QuizArgs(BaseModel):
    topic: str | None = Field(
        default=None,
        max_length=200,
        description="What to test. Defaults to the room's title.",
        examples=["photosynthesis"],
    )
    question_count: int = Field(
        default=5,
        ge=MIN_QUESTIONS,
        le=MAX_QUESTIONS,
        description="How many questions to write.",
    )


# --- what we ask the model for ----------------------------------------------


class DraftChoice(BaseModel):
    text: str
    is_correct: bool


class DraftQuestion(BaseModel):
    stem: str
    choices: list[DraftChoice]
    explanation: str
    source: str | None = None


class DraftQuiz(BaseModel):
    """Flat on purpose — nested `$defs` are handled inconsistently by providers."""

    questions: list[DraftQuestion]


# --- the checks --------------------------------------------------------------


def check_quiz(quiz: DraftQuiz, *, expected: int) -> list[str]:
    """Everything wrong with this quiz, written for the model to read.

    Every problem is reported, not just the first: one repair call fixing four
    faults beats four calls fixing one each.
    """
    problems: list[str] = []

    if len(quiz.questions) != expected:
        problems.append(
            f"there are {len(quiz.questions)} questions but exactly {expected} were asked for"
        )

    for index, question in enumerate(quiz.questions, 1):
        if not question.stem.strip():
            problems.append(f"question {index} has an empty question line")
        if not question.explanation.strip():
            problems.append(f"question {index} has no explanation of the right answer")

        if len(question.choices) != CHOICES_PER_QUESTION:
            problems.append(
                f"question {index} has {len(question.choices)} options but must have "
                f"exactly {CHOICES_PER_QUESTION}"
            )

        correct = [choice for choice in question.choices if choice.is_correct]
        if len(correct) != 1:
            problems.append(
                f"question {index} has {len(correct)} options marked correct but must "
                f"have exactly 1"
            )

        seen: dict[str, int] = {}
        for position, choice in enumerate(question.choices, 1):
            if not choice.text.strip():
                problems.append(f"question {index}, option {position} is blank")
                continue
            key = normalise(choice.text)
            if key in seen:
                problems.append(
                    f"question {index} options {seen[key]} and {position} are the same "
                    f"answer written twice ({choice.text!r})"
                )
            else:
                seen[key] = position

    return problems


def balance_positions(quiz: DraftQuiz, *, seed: int) -> DraftQuiz:
    """Shuffle each question's options so the answer is not always in one slot.

    The fix for the fourth failure, and the one that cannot be a check. A model's
    tendency to put the correct answer in the same position is a property of many
    quizzes, so no single quiz can be rejected for it — and telling the model to
    vary the position does not work, because it is not disobeying an instruction.

    Shuffling makes the property true by construction. The seed comes from the
    run, so the same quiz always shuffles the same way and a test can assert on
    the result.
    """
    rng = random.Random(seed)
    shuffled = []
    for question in quiz.questions:
        choices = list(question.choices)
        rng.shuffle(choices)
        shuffled.append(question.model_copy(update={"choices": choices}))
    return quiz.model_copy(update={"questions": shuffled})


class QuizBuilderSkill(Skill):
    name = "quiz_builder"
    version = PROMPT_VERSION
    args_model: ClassVar[type[BaseModel]] = QuizArgs

    def describe_request(self, args: QuizArgs) -> str:
        topic = args.topic or "this room's topic"
        return f"Build me a {args.question_count}-question quiz on {topic}."

    def summarise(self, args: QuizArgs, payload: DraftQuiz) -> str:
        sourced = sum(1 for question in payload.questions if question.source)
        grounded = (
            f", {sourced} of them from your uploaded material" if sourced else ""
        )
        return (
            f"Here is a {len(payload.questions)}-question quiz on "
            f"{args.topic or 'this room'}{grounded}."
        )

    def concepts(self, args: QuizArgs, payload: DraftQuiz) -> list[str]:
        """The topic the student named, so a quiz counts in study history.

        One tag, not one per question: the student studied the topic, and
        tagging each question would flood the concept counts with a granularity
        no conversation turn produces.
        """
        topic = (args.topic or "").strip()
        return [topic.lower()] if topic else []

    async def run(self, args: QuizArgs, context: SkillContext) -> Generation:
        topic = (args.topic or context.room.title).strip()

        # Grounded in the student's own material when there is any. Their course
        # defines the terms they are marked on, so a quiz drawn from the handout
        # is worth more than one drawn from the subject at large.
        hits = await document_service.search(context.db, context.room, topic, limit=6)
        passages = [{"cite": hit.citation, "text": hit.text} for hit in hits]
        logger.info(
            "quiz on %r in room %s: %d passages of material", topic, context.room.id, len(passages)
        )

        result = await generate(
            context,
            prompt_name=PROMPT_NAME,
            prompt_version=PROMPT_VERSION,
            variables={
                "education_level": context.student.education_level,
                "room_title": context.room.title,
                "topic": topic,
                "question_count": args.question_count,
                "choice_count": CHOICES_PER_QUESTION,
                "passages": passages,
            },
            schema=DraftQuiz,
            check=lambda quiz: check_quiz(quiz, expected=args.question_count),
            repair_prompt=REPAIR_INSTRUCTION,
        )

        # After the checks, never before: shuffling a quiz that is about to be
        # rejected would waste the work, and the checks do not care about order.
        result.payload = balance_positions(result.payload, seed=_seed(topic, args))
        return result


def _seed(topic: str, args: QuizArgs) -> int:
    """A seed derived from the request, so the same request shuffles the same way.

    Deterministic on purpose: unseeded shuffling would make every run of the test
    suite a different quiz, and a flaky assertion is worse than a predictable one.
    The property being tested is "the answer moves off whichever slot the model
    chose", not "the order is unguessable to an attacker".

    `hashlib`, not the built-in `hash()`. Python randomises string hashing per
    process unless `PYTHONHASHSEED` is set, so `hash()` here would be stable
    *within* a run and different on the next one — which is the exact failure
    this function exists to avoid, and it would have shown up as a test that
    passes until it does not.
    """
    digest = hashlib.sha256(f"{topic}|{args.question_count}".encode()).digest()
    return int.from_bytes(digest[:4], "big")
