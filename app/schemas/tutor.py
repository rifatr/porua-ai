"""The shape the tutor must reply in.

This is the *schema* layer of the reliability pipeline: the JSON parsed, but is it
the right JSON? Pydantic answers that and nothing else. Whether the answer is any
good is a separate question, asked afterwards by `app/reliability/checks.py`.

Keeping those two apart is deliberate. Schema failures are the model getting the
container wrong and are usually fixed by restating the format. Content failures
are the model getting the substance wrong and need a different repair message. If
one layer reported both, the repair prompt could not be specific.

Every field here has a reader. See `docs/ASSUMPTIONS.md` — a field the tutor fills
in that nothing ever consumes is a cost with no benefit, paid on every turn.
"""

from pydantic import BaseModel, Field


class TutorAnswer(BaseModel):
    """One tutor reply, before any content checking.

    `extra` is left at Pydantic's default of "ignore" rather than "forbid". If the
    model volunteers a `confidence` field we did not ask for, that is harmless,
    and failing on it would spend a billable repair call to remove a key we were
    going to drop anyway.
    """

    answer: str = Field(description="The reply shown to the student. Markdown.")

    in_scope: bool = Field(
        description=(
            "Did the question belong in this room? False means the tutor declined "
            "because the question was about another subject."
        )
    )

    # Required, with no default, on purpose. An omitted list and an empty list are
    # not the same statement: one is the model forgetting, the other is the model
    # saying "nothing was studied here". Requiring the key removes the ambiguity
    # for the cost of two characters.
    concepts: list[str] = Field(
        description=(
            "Short topic tags for what this turn actually taught, e.g. "
            "['inverse operations', 'linear equations']. Empty when the turn "
            "taught nothing — a greeting, or an off-topic question."
        )
    )
