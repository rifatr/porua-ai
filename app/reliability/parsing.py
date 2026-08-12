"""Getting a JSON object out of whatever the model actually sent.

The prompt says "reply with a single JSON object and nothing else". Models
sometimes do. The rest of the time they wrap it in ```json fences, introduce it
with "Here is the JSON:", add a closing remark, or run out of output budget
halfway through the answer.

The dividing line used throughout this module: **recover what can be recovered
without guessing at meaning; fail everything else.**

Stripping a code fence is safe — the content inside is untouched and no
interpretation is involved. Closing a truncated object is not: we would be
inventing the end of a sentence the student is about to read. So fences are
repaired here for free, and truncation costs a repair call.

That line is worth defending. Every recovery done in code is a paid model call
saved on every single turn; every recovery done by guessing is a chance to save
something the model never said.
"""

import json
import logging
import re

from app.reliability.failures import Layer, ResponseInvalid, ValidationFailure

logger = logging.getLogger(__name__)

# ```json … ``` or plain ``` … ```. Non-greedy so the first block wins.
_FENCED = re.compile(r"```[a-zA-Z]*\s*\n?(.*?)```", re.DOTALL)


def _fail(code: str, detail: str) -> ResponseInvalid:
    return ResponseInvalid([ValidationFailure(layer=Layer.PARSE, code=code, detail=detail)])


def _strip_fences(text: str) -> str:
    match = _FENCED.search(text)
    return match.group(1) if match else text


def _first_object(text: str) -> str | None:
    """The first balanced {...} block, or None if there is not one.

    Brace counting rather than a regular expression, because the answer field
    holds Markdown that can itself contain braces, and quotes that can contain
    braces inside them. A regex would stop at the first `}` it saw and cut a
    valid answer in half.
    """
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False

    for index in range(start, len(text)):
        char = text[index]

        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]

    # Ran off the end with braces still open: the response was cut short.
    return None


def _loads(text: str):
    """json.loads with one fallback. Raises JSONDecodeError if both passes fail.

    The fallback relaxes exactly one rule: literal control characters inside
    strings. That is precisely how the model fails here, by writing Markdown with
    real line breaks instead of `\\n`. The text is taken exactly as sent and
    nothing is inferred, which is what makes recovering it safe — and it turns the
    single most common malformation into no cost at all.
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        value = json.loads(text, strict=False)
        logger.info("recovered malformed json with a non-strict parse")
        return value


def extract_json(raw: str) -> dict:
    """The parse layer. Returns a dict or raises ResponseInvalid."""
    text = _strip_fences(raw).strip()

    if not text:
        raise _fail("EMPTY_RESPONSE", "The response was empty. Reply with a JSON object.")

    # The whole response first. This is the clean case, and it is also the only
    # way to notice that the model sent a JSON *array* — searching for an embedded
    # object would find the first element and silently drop the rest.
    try:
        payload = _loads(text)
    except json.JSONDecodeError:
        # Not JSON as a whole, but it may still contain some: a preamble, a
        # closing remark, a stray sentence after the object.
        candidate = _first_object(text)
        if candidate is None:
            if "{" in text:
                raise _fail(
                    "JSON_TRUNCATED",
                    "The JSON object was never closed — the response stopped "
                    "partway through. Reply again with the complete object, and "
                    "keep the answer shorter so it fits.",
                ) from None
            raise _fail(
                "NO_JSON_FOUND",
                "The response contained no JSON object at all. Reply with a "
                "single JSON object and nothing else.",
            ) from None

        try:
            payload = _loads(candidate)
        except json.JSONDecodeError as exc:
            raise _fail(
                "JSON_MALFORMED",
                f"The JSON could not be parsed: {exc.msg} at line {exc.lineno} "
                f"column {exc.colno}. Check every quote inside the answer is "
                f'escaped as \\" and every line break as \\n.',
            ) from exc

    if not isinstance(payload, dict):
        # An array holding one object is tempting to unwrap. We do not: with two
        # elements there is no way to know which one was meant, and picking the
        # first would quietly discard an answer the model wrote.
        raise _fail(
            "JSON_NOT_AN_OBJECT",
            f"Expected a JSON object but got a {type(payload).__name__}. "
            "Reply with a single object using the keys shown in the format, not "
            "a list.",
        )

    return payload
