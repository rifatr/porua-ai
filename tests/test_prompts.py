"""The prompt loader.

Small, but every stored attempt references a prompt name, version and checksum.
If that link is wrong, the inspection endpoint is quietly lying about what
produced an answer.
"""

import pytest
from jinja2 import UndefinedError

from app.prompts.loader import PromptError, latest_version, load_prompt


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
