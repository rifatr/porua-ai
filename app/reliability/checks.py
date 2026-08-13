"""Fixing what code can fix, then rejecting only what it cannot.

The brief names this problem directly — "structurally valid but poor content" —
and it is the half schema validation cannot reach.

## Why there are only three checks

A repair costs real money, costs the student several seconds of waiting, and
**might not work**. A line of Python costs nothing and **always** works. So the
rule here is:

> **Reject only what code cannot correct.**

Duplicate tags, blank strings, tags on a turn that taught nothing — every one of
those is a string operation. Asking a language model to perform it, and paying for
the privilege, is indefensible. `tidy()` does them below, for free.

Rules that belong to neither list, and are therefore absent:

- *no "Great question!" opening* — taste, not correctness. A prefix match also
  catches "Absolutely convergent series…", which is a correct sentence.
- *no `#` headings* — taste, and demoting a heading is a one-line rewrite.
- *at most 4 concepts* — nine tags instead of four harms nobody.
- *no concept longer than 60 characters* — barely harms anybody.

A check earns its place by protecting the student or the data. Anything else is
an opinion with a bill attached.

## Why the word limit is so much higher than the prompt's

The prompt asks for 250 words. This rejects at 800, and the gap is the point. The
prompt itself says "at most 250 words **unless the student explicitly asks for more
detail**" — and this function never sees the student's message. A student who asks
for depth, gets 380 well-judged words, and has them thrown away and replaced with
something shorter has been actively harmed by the check meant to help them.

So this is not a style rule. It is a runaway guard: no answer pitched at a school
student is 800 words, so at that length the model has lost the plot, and nothing a
student legitimately asked for is caught.
"""

import logging

from app.reliability.failures import Layer, ValidationFailure
from app.schemas.tutor import TutorAnswer

logger = logging.getLogger(__name__)

# A runaway guard, not a style rule. See the module docstring.
MAX_ANSWER_WORDS = 800

# A decline is different: the prompt asks for under 40 words, and going long here
# means the model declined and then answered anyway. Enforced tightly because the
# whole rule collapses otherwise.
MAX_DECLINE_WORDS = 60


def normalise(text: str) -> str:
    """Lowercase, trim, collapse runs of whitespace.

    The comparison key for "are these two strings the same thing". `"Paris"`,
    `" paris "` and `"PARIS"` all reduce to `paris`, which makes duplicate
    detection match a human's idea of a duplicate rather than a byte comparison.
    S6 needs exactly this for quiz options.
    """
    return " ".join(text.split()).lower()


def tidy(answer: TutorAnswer) -> TutorAnswer:
    """Correct in code everything that does not need the model's opinion.

    Runs before the checks below, so nothing here can ever cause a repair. Each
    correction is mechanical and cannot lose meaning:

    - blank tags are dropped — an empty string names no concept;
    - duplicates are removed, keeping the first spelling the model chose, so
      study history counts a concept once rather than twice;
    - an out-of-scope turn has its tags cleared, because a turn that declined to
      answer taught nothing about this room's subject. That is not a judgement
      call, it is what `in_scope: false` means.

    The unmodified response is still stored on the attempt as `raw_response`, so
    the before and after remain visible to anyone inspecting the turn.
    """
    concepts: list[str] = []
    seen: set[str] = set()

    if answer.in_scope:
        for concept in answer.concepts:
            key = normalise(concept)
            if not key or key in seen:
                continue
            seen.add(key)
            concepts.append(concept.strip())

    if concepts != answer.concepts:
        logger.info(
            "tidied concepts %r -> %r (in_scope=%s)",
            answer.concepts,
            concepts,
            answer.in_scope,
        )

    return answer.model_copy(update={"concepts": concepts})


def _failure(code: str, detail: str, field: str | None = None) -> ValidationFailure:
    return ValidationFailure(layer=Layer.CONTENT, code=code, detail=detail, field=field)


def check_tutor_answer(answer: TutorAnswer) -> list[ValidationFailure]:
    """Everything wrong with the reply that code could not put right itself.

    Empty list means it is acceptable. All rules run rather than returning at the
    first — one repair call fixing three problems beats three calls fixing one
    each.
    """
    failures: list[ValidationFailure] = []
    text = answer.answer.strip()
    words = len(text.split())

    if not text:
        # Nothing to correct: there is no answer to correct.
        failures.append(_failure("ANSWER_EMPTY", "The answer was blank.", field="answer"))

    if words > MAX_ANSWER_WORDS:
        failures.append(
            _failure(
                "ANSWER_TOO_LONG",
                f"The answer is {words} words, which is far past anything a "
                f"student asked for. Answer the question directly, in under 250 "
                f"words unless more detail was requested.",
                field="answer",
            )
        )

    if not answer.in_scope and words > MAX_DECLINE_WORDS:
        # The model says it will not answer, then answers. Nothing in code can
        # shorten prose without losing meaning, and letting it through would make
        # the room-scoping rule a fiction the student can see straight through.
        failures.append(
            _failure(
                "DECLINE_TOO_LONG",
                f"in_scope is false but the reply is {words} words. A decline "
                f"should be under 40 words: say this room is for its topic and "
                f"suggest a new room. Do not answer the question anyway.",
                field="answer",
            )
        )

    return failures
