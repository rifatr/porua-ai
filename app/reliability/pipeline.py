"""The layers, run in order.

    parse  ->  schema  ->  tidy  ->  check  ->  (repair, in services/turn.py)

One function, so that every caller checks a response the same way and there is a
single place to point at on the review call.

`tidy` sits deliberately between validating and checking. It corrects everything
code can correct — duplicate tags, blanks, tags on a turn that taught nothing — so
that by the time the checks run, the only things left to object to are things a
model has to fix. Ordering it after the checks would be pointless; ordering it
before the schema is impossible, because there is nothing typed to tidy yet.

The layers stop at the first failure rather than accumulating across all of them,
and that is not laziness. If the response is not JSON there is nothing to check
the word count of; if a required field is missing the content rules cannot read
it. Reporting "not valid JSON" *and* "answer too long" would be reporting a
consequence as if it were a second, independent problem, and the repair prompt
would be arguing with the model about something that was never true.

Within a layer, every failure is reported at once — see ResponseInvalid.
"""

from pydantic import ValidationError

from app.reliability.checks import check_tutor_answer, tidy
from app.reliability.failures import Layer, ResponseInvalid, ValidationFailure
from app.reliability.parsing import extract_json
from app.schemas.tutor import TutorAnswer


def _schema_failures(error: ValidationError) -> list[ValidationFailure]:
    """Pydantic's report, translated into ours.

    Pydantic already says exactly what is wrong and where. Restating it in our own
    words would only let the two drift apart, so the message is passed through and
    only the location is reformatted into something a model can act on.
    """
    failures = []
    for detail in error.errors():
        field = ".".join(str(part) for part in detail["loc"]) or None
        failures.append(
            ValidationFailure(
                layer=Layer.SCHEMA,
                # e.g. "missing" -> MISSING, "bool_type" -> BOOL_TYPE
                code=str(detail["type"]).upper(),
                detail=detail["msg"],
                field=field,
            )
        )
    return failures


def validate_tutor_response(raw: str) -> TutorAnswer:
    """Turn a raw model response into a checked answer, or explain why not.

    Raises ResponseInvalid carrying the failures from whichever layer rejected it.
    Those failures go two places: onto the attempt row, so the inspection endpoint
    can show them, and into the repair prompt, so the next call is told precisely
    what to fix.
    """
    payload = extract_json(raw)

    try:
        answer = TutorAnswer.model_validate(payload)
    except ValidationError as exc:
        raise ResponseInvalid(_schema_failures(exc)) from exc

    # Fix what code can fix *before* checking, so nothing correctable ever
    # triggers a repair. This is the difference between a duplicate tag costing
    # nothing and costing a billable call plus three seconds of the student's
    # time — for something `dict.fromkeys` solves.
    answer = tidy(answer)

    if failures := check_tutor_answer(answer):
        raise ResponseInvalid(failures)

    return answer
