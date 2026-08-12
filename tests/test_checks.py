"""Fixing what code can fix, then rejecting only what it cannot.

Two halves, and the split is the design:

`tidy` corrects things for free. Every test in that section is a repair call that
never happens — money not spent, and seconds the student does not wait.

`check_tutor_answer` rejects the three things left over. Every one of them would
reach the student as something visibly wrong, and none can be corrected without
inventing content.
"""

import json

from app.reliability.checks import (
    MAX_ANSWER_WORDS,
    check_tutor_answer,
    normalise,
    tidy,
)
from app.reliability.failures import Layer
from app.schemas.tutor import TutorAnswer
from tests.synthetic import synthetic


def answer(text: str = "Subtract 6, then divide by 3.", **overrides) -> TutorAnswer:
    return TutorAnswer(
        answer=text,
        in_scope=overrides.pop("in_scope", True),
        concepts=overrides.pop("concepts", ["inverse operations"]),
    )


def from_fixture(name: str) -> TutorAnswer:
    return TutorAnswer.model_validate(json.loads(synthetic(name)))


def codes(response: TutorAnswer) -> list[str]:
    return [f.code for f in check_tutor_answer(response)]


# --- corrected in code, so no repair is ever triggered ----------------------


def test_duplicate_tags_are_removed_rather_than_sent_back() -> None:
    """A duplicate tag is `dict.fromkeys`. Paying a model to remove it is not a
    reliability measure, it is a bill."""
    tidied = tidy(from_fixture("duplicate_concepts.json"))
    assert tidied.concepts == ["inverse operations", "linear equations"]
    assert check_tutor_answer(tidied) == [], "nothing left to repair"


def test_the_first_spelling_the_model_chose_is_kept() -> None:
    """Deduping must not silently reformat what survives."""
    assert tidy(answer(concepts=["Inverse Operations", "inverse operations"])).concepts == [
        "Inverse Operations"
    ]


def test_blank_tags_are_dropped() -> None:
    assert tidy(answer(concepts=["algebra", "   ", ""])).concepts == ["algebra"]


def test_surrounding_spaces_are_trimmed() -> None:
    assert tidy(answer(concepts=["  algebra  "])).concepts == ["algebra"]


def test_an_out_of_scope_turn_has_its_tags_cleared() -> None:
    """Not a judgement call — it is what `in_scope: false` means. A turn that
    declined to answer taught nothing about this room's subject, so asking the
    model to agree would spend a call to be told something we already know."""
    tidied = tidy(answer("Wrong room for that.", in_scope=False, concepts=["football"]))
    assert tidied.concepts == []


def test_tidy_leaves_a_good_answer_alone() -> None:
    original = answer(concepts=["inverse operations", "linear equations"])
    assert tidy(original).concepts == original.concepts


def test_normalise_makes_case_and_spacing_irrelevant() -> None:
    """The comparison key behind deduping. S6 reuses it for quiz options."""
    assert normalise("  Inverse   Operations ") == normalise("inverse operations")


# --- what is left: rejected, because code cannot correct it ------------------


def test_a_good_answer_has_no_failures() -> None:
    assert check_tutor_answer(answer()) == []


def test_a_blank_answer_is_rejected() -> None:
    """Nothing to correct. There is no answer to correct."""
    assert "ANSWER_EMPTY" in codes(answer("   "))


def test_a_long_answer_a_student_asked_for_is_accepted() -> None:
    """The prompt allows more than 250 words when the student asks for detail, and
    this function never sees the student's message. Rejecting a well-judged
    380-word answer would replace it with a worse one at the student's expense —
    the check would be harming the person it exists to protect."""
    assert codes(answer(" ".join(["word"] * 380))) == []


def test_a_runaway_answer_is_rejected() -> None:
    """At this length the model has lost the plot, not answered thoroughly."""
    assert "ANSWER_TOO_LONG" in codes(answer(" ".join(["word"] * (MAX_ANSWER_WORDS + 1))))


def test_a_short_decline_is_accepted() -> None:
    short = answer(
        "This room is for Grade 7 Algebra. Try opening a new room for that topic.",
        in_scope=False,
        concepts=[],
    )
    assert codes(short) == []


def test_declining_and_then_answering_anyway_is_rejected() -> None:
    """The one content rule that genuinely needs a model call.

    The model says it will not answer the question, then answers it in full. Prose
    cannot be shortened in code without losing meaning, and letting it through
    would make the room-scoping rule a fiction the student can see through.
    """
    assert "DECLINE_TOO_LONG" in codes(from_fixture("decline_but_answers_anyway.json"))


def test_the_failure_detail_is_specific_enough_to_act_on() -> None:
    """It goes into the repair prompt verbatim. "Invalid response" produces
    another invalid response."""
    failures = check_tutor_answer(from_fixture("decline_but_answers_anyway.json"))
    detail = next(f.detail for f in failures if f.code == "DECLINE_TOO_LONG")
    assert "138 words" in detail
    assert "under 40 words" in detail


def test_failures_are_tagged_as_content_failures() -> None:
    """The layer is shown by the inspection endpoint, so it has to be right."""
    failures = check_tutor_answer(from_fixture("decline_but_answers_anyway.json"))
    assert all(f.layer is Layer.CONTENT for f in failures)


def test_every_rule_is_reported_at_once() -> None:
    """One repair call fixing two problems beats two calls fixing one each."""
    both = answer(" ".join(["word"] * 900), in_scope=False, concepts=[])
    assert set(codes(both)) == {"ANSWER_TOO_LONG", "DECLINE_TOO_LONG"}
