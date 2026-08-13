"""The prompt loader.

Small, but every stored attempt references a prompt name, version and checksum.
If that link is wrong, the inspection endpoint is quietly lying about what
produced an answer.
"""

import pytest
from jinja2 import UndefinedError

from app.prompts.loader import PromptError, latest_version, load_prompt
from app.services.turn import (
    REPAIR_PROMPT,
    REPAIR_PROMPT_VERSION,
    TUTOR_PROMPT,
    TUTOR_PROMPT_VERSION,
)


def test_loads_the_tutor_prompt() -> None:
    prompt = load_prompt("tutor_system", 1)
    assert prompt.name == "tutor_system"
    assert prompt.version == 1
    assert prompt.model == "gemini-2.5-flash"
    assert len(prompt.sha256) == 64


def test_rendering_substitutes_the_variables() -> None:
    rendered = load_prompt("tutor_system", 1).render(
        education_level="Class 8",
        room_title="Grade 7 Algebra",
        recent_turns=[],
        student_message="What is a variable?",
    )
    assert "Class 8" in rendered
    assert "Grade 7 Algebra" in rendered
    assert "What is a variable?" in rendered


def test_a_missing_variable_raises_instead_of_rendering_a_gap() -> None:
    """StrictUndefined. Silently rendering an empty string would give a worse
    prompt and no error anywhere — the failure would only show up as bad answers."""
    with pytest.raises(UndefinedError):
        load_prompt("tutor_system", 1).render(room_title="Algebra")


def test_the_checksum_is_stable_across_loads() -> None:
    assert load_prompt("tutor_system", 1).sha256 == load_prompt("tutor_system", 1).sha256


def test_a_missing_prompt_is_an_error() -> None:
    with pytest.raises(PromptError):
        load_prompt("no_such_prompt", 1)

    with pytest.raises(PromptError):
        load_prompt("tutor_system", 99)


def test_latest_version_finds_the_highest_on_disk() -> None:
    assert latest_version("tutor_system") >= 1


@pytest.mark.parametrize(
    ("name", "pinned"),
    [
        (TUTOR_PROMPT, TUTOR_PROMPT_VERSION),
        (REPAIR_PROMPT, REPAIR_PROMPT_VERSION),
    ],
)
def test_the_pinned_prompt_is_the_newest_one_written(name: str, pinned: int) -> None:
    """Writing a new prompt version and not switching to it leaves it inert.

    That is exactly what happened in S5: `tutor_system` v4 introduced the
    `search_room_materials` tool, the constant still said 3, and v3 tells the
    model it has two tools — so the third was declared to the provider and talked
    out of existence by the prompt. Nothing failed. Answers were merely worse, and
    the only evidence was `prompt_version: 3` on a stored attempt.

    The version stays pinned rather than resolved from disk, because a prompt
    change is a behavioural change and should be a reviewed commit. This test is
    the other half of that trade: pinning is deliberate, forgetting is not.
    """
    assert pinned == latest_version(name), (
        f"{name} v{latest_version(name)} exists but the code still uses "
        f"v{pinned}. Bump the constant in app/services/turn.py, or delete the "
        f"unused prompt file."
    )
