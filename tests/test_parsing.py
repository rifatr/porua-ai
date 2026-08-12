"""The parse layer.

The dividing line these tests hold in place: **recover what can be recovered
without guessing; fail everything else.** Each recovery below is a billable repair
call saved on every turn that hits it, and each rejection is a place where
recovering would have meant inventing text the model never sent.
"""

import pytest

from app.reliability.failures import Layer, ResponseInvalid
from app.reliability.parsing import extract_json
from tests.synthetic import synthetic


def _codes(error: ResponseInvalid) -> list[str]:
    return [f.code for f in error.failures]


# --- recovered without a second call ----------------------------------------


def test_plain_json_is_parsed() -> None:
    assert extract_json('{"answer": "hi", "in_scope": true, "concepts": []}')["answer"] == "hi"


def test_code_fences_are_stripped() -> None:
    """Models wrap JSON in ```json out of habit. That is formatting, not failure."""
    payload = extract_json(synthetic("json_in_fences.txt"))
    assert payload["in_scope"] is True
    assert payload["concepts"] == ["inverse operations", "linear equations"]


def test_a_fence_without_a_language_tag_is_also_stripped() -> None:
    assert extract_json('```\n{"answer": "hi"}\n```')["answer"] == "hi"


def test_chatter_before_and_after_the_object_is_ignored() -> None:
    payload = extract_json(synthetic("preamble_and_trailer.txt"))
    assert payload["answer"].startswith("Subtract 6")


def test_literal_line_breaks_inside_a_string_are_recovered() -> None:
    """The single most common malformation: Markdown written with real newlines.

    Strict JSON forbids control characters inside strings, so this fails a normal
    parse. The non-strict second pass takes the text exactly as sent and infers
    nothing, which is what makes recovering it safe rather than a guess.
    """
    payload = extract_json(synthetic("literal_newlines.txt"))
    assert "3x = 9" in payload["answer"]
    assert payload["concepts"] == ["inverse operations", "checking a solution"]


def test_braces_inside_the_answer_do_not_end_the_object() -> None:
    """Why the scanner counts braces instead of matching a regex.

    A tutor explaining set notation writes braces into its answer. A regex ending
    at the first `}` would cut the answer in half and call the result valid.
    """
    raw = '{"answer": "A set is written {1, 2, 3} like this.", "in_scope": true, "concepts": []}'
    assert extract_json(raw)["answer"].endswith("like this.")


def test_a_quoted_brace_does_not_confuse_the_scanner() -> None:
    raw = '{"answer": "Type \\"}\\" to close it.", "in_scope": true, "concepts": []}'
    assert extract_json(raw)["in_scope"] is True


# --- rejected, because recovering would mean inventing content ---------------


def test_truncated_json_is_rejected_not_repaired_in_code() -> None:
    """We could close the braces ourselves. We would be writing the end of a
    sentence the student is about to read, so we ask the model instead."""
    with pytest.raises(ResponseInvalid) as caught:
        extract_json(synthetic("truncated.txt"))
    assert _codes(caught.value) == ["JSON_TRUNCATED"]
    assert caught.value.layer is Layer.PARSE


def test_prose_with_no_json_is_rejected() -> None:
    with pytest.raises(ResponseInvalid) as caught:
        extract_json(synthetic("prose_not_json.txt"))
    assert _codes(caught.value) == ["NO_JSON_FOUND"]


def test_a_json_array_is_rejected() -> None:
    with pytest.raises(ResponseInvalid) as caught:
        extract_json(synthetic("json_array.txt"))
    assert _codes(caught.value) == ["JSON_NOT_AN_OBJECT"]


def test_an_empty_response_is_rejected() -> None:
    with pytest.raises(ResponseInvalid) as caught:
        extract_json("   \n  ")
    assert _codes(caught.value) == ["EMPTY_RESPONSE"]


def test_genuinely_broken_json_is_rejected_with_a_position() -> None:
    """The detail goes into the repair prompt, so it has to say where to look."""
    with pytest.raises(ResponseInvalid) as caught:
        extract_json('{"answer": "hi" "in_scope": true}')
    assert _codes(caught.value) == ["JSON_MALFORMED"]
    assert "column" in caught.value.failures[0].detail
