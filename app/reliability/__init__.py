"""Everything that stands between a model response and a saved answer.

The brief's instruction is to "assume the Gemini model will sometimes ignore
instructions, return malformed data, or produce structurally valid but poor
content", and those are three different problems, so this package answers them in
three separate layers:

    parsing.py   the response is not JSON            -> extract it, or fail
    ../schemas/tutor.py
                 it is JSON, but the wrong JSON      -> Pydantic
    checks.py    it is the right JSON, but bad       -> our own rules

`pipeline.py` runs them in that order and stops at the first layer that fails,
because a response that is not JSON cannot also be checked for word count, and
reporting both would only confuse the repair prompt.

Retries and repairs are *not* here. They live in `services/turn.py`, because
deciding to call the model again is a decision about the turn — it costs money,
writes a row, and has a budget. This package only ever answers "is this response
acceptable, and if not, exactly what is wrong with it".
"""
