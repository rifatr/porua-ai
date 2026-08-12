"""The whole pipeline, through the API.

`test_parsing.py` and `test_checks.py` test the layers in isolation. This file
tests the thing that actually matters: given a model that misbehaves, what reaches
the student and what gets written down.

The single most important assertion in the suite is
`test_nothing_invalid_is_ever_saved_or_returned`. Everything else here is detail
around it. If that one passes, the claim the project rests on — a turn either
carries a checked answer or it carries nothing and says why — is true.
"""

import json
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_llm
from app.llm.base import EmptyResponse, ProviderUnavailable, ResponseBlocked
from app.main import app
from app.models.room import Room
from app.models.student import Student
from app.models.turn import Turn, TurnFailureReason
from tests.fakes import FakeLLM, tutor_json
from tests.synthetic import synthetic

GOOD = tutor_json()
JUNK = synthetic("prose_not_json.txt")


def use_llm(llm: FakeLLM) -> FakeLLM:
    """Swap the model for this test. The client fixture clears it afterwards."""
    app.dependency_overrides[get_llm] = lambda: llm
    return llm


async def make_room(client: AsyncClient, auth: dict[str, str], title: str = "Algebra") -> str:
    return (await client.post("/rooms", json={"title": title}, headers=auth)).json()["id"]


async def run_turn(
    client: AsyncClient,
    auth: dict[str, str],
    room_id: str,
    message: str = "How do I solve 3x + 6 = 15?",
) -> dict:
    response = await client.post(
        f"/rooms/{room_id}/turns", json={"message": message}, headers=auth
    )
    assert response.status_code == 201, response.text
    return response.json()


async def inspect(client: AsyncClient, auth: dict[str, str], turn_id: str) -> dict:
    return (await client.get(f"/turns/{turn_id}", headers=auth)).json()


# --- recovered in code, with no second call ---------------------------------


async def test_a_fenced_response_costs_no_repair_call(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    """Stripping a code fence is free. Asking the model to strip it is not.

    This is the difference between a pipeline that costs one call per turn and one
    that costs two, on the single most common formatting habit models have.
    """
    llm = use_llm(FakeLLM(answers=[synthetic("json_in_fences.txt")]))
    room_id = await make_room(client, auth)

    turn = await run_turn(client, auth, room_id)

    assert turn["status"] == "succeeded"
    assert len(llm.calls) == 1, "no repair should have been needed"
    assert turn["answer_text"].startswith("Subtract 6")


# --- the repair loop --------------------------------------------------------


async def test_one_bad_response_is_repaired_and_the_student_never_sees_it(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    llm = use_llm(FakeLLM(answers=[JUNK, GOOD]))
    room_id = await make_room(client, auth)

    turn = await run_turn(client, auth, room_id)

    assert turn["status"] == "succeeded"
    assert turn["answer_text"] == json.loads(GOOD)["answer"]
    assert len(llm.calls) == 2

    detail = await inspect(client, auth, turn["id"])
    assert [a["purpose"] for a in detail["attempts"]] == ["tutor", "repair"]
    assert [a["prompt_name"] for a in detail["attempts"]] == ["tutor_system", "tutor_repair"]


async def test_duplicate_tags_are_deduped_in_code_and_cost_nothing(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    """The cheapest reliability measure in the project.

    Removing a repeated tag is `dict.fromkeys`. Sending the response back to
    Gemini to have it removed would cost money and several seconds of the
    student's wait, and might not even work. Code always works.
    """
    llm = use_llm(FakeLLM(answers=[synthetic("duplicate_concepts.json")]))
    room_id = await make_room(client, auth)

    turn = await run_turn(client, auth, room_id)

    assert turn["status"] == "succeeded"
    assert turn["concepts"] == ["inverse operations", "linear equations"]
    assert len(llm.calls) == 1, "no repair — this was fixed without asking"


async def test_the_rejected_attempt_records_why_it_was_rejected(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    """The brief asks to inspect a turn's failures. A repaired turn has one, and
    it would be invisible if only the surviving attempt were kept."""
    use_llm(FakeLLM(answers=[synthetic("decline_but_answers_anyway.json"), GOOD]))
    room_id = await make_room(client, auth)

    turn = await run_turn(client, auth, room_id, "Who won the 2022 world cup?")
    first, second = (await inspect(client, auth, turn["id"]))["attempts"]

    assert first["validation_failures"] is not None
    assert first["validation_failures"][0]["layer"] == "content"
    assert first["validation_failures"][0]["code"] == "DECLINE_TOO_LONG"
    assert first["raw_response"] is not None, "the rejected text is kept, not discarded"
    assert second["validation_failures"] is None, "the accepted attempt has none"


async def test_the_repair_prompt_names_the_problem_and_shows_the_rejection(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    """A repair prompt that says only "that was invalid" produces another invalid
    response. Specificity is the whole mechanism."""
    llm = use_llm(FakeLLM(answers=[synthetic("decline_but_answers_anyway.json"), GOOD]))
    room_id = await make_room(client, auth)
    await run_turn(client, auth, room_id, "Who won the 2022 world cup?")

    repair_prompt = llm.prompts[1]
    assert "DECLINE_TOO_LONG" in repair_prompt
    assert "138 words" in repair_prompt, "the problem is quantified, not just named"
    assert "<rejected_output>" in repair_prompt, "the model can see its own attempt"
    assert llm.calls[1].temperature < llm.calls[0].temperature, "repairs run colder"


async def test_two_bad_responses_are_still_repaired(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    llm = use_llm(FakeLLM(answers=[JUNK, synthetic("missing_concepts.json"), GOOD]))
    room_id = await make_room(client, auth)

    turn = await run_turn(client, auth, room_id)

    assert turn["status"] == "succeeded"
    assert len(llm.calls) == 3

    detail = await inspect(client, auth, turn["id"])
    layers = [a["validation_failures"][0]["layer"] for a in detail["attempts"][:2]]
    assert layers == ["parse", "schema"], "each layer rejected in turn"


async def test_nothing_invalid_is_ever_saved_or_returned(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    """The claim the project rests on.

    The model returns something plausible-looking every single time, and every
    version of it is unacceptable. The student gets nothing, and the record says
    exactly why. There is no middle state where an unchecked answer gets through
    because we ran out of patience.
    """
    llm = use_llm(FakeLLM(answers=[JUNK, JUNK, JUNK, GOOD]))
    room_id = await make_room(client, auth)

    turn = await run_turn(client, auth, room_id)

    assert turn["status"] == "failed"
    assert turn["failure_reason"] == "INVALID_AFTER_REPAIR"
    assert turn["answer_text"] is None
    assert turn["in_scope"] is None
    assert len(llm.calls) == 3, "one call plus two repairs, then it stops"

    detail = await inspect(client, auth, turn["id"])
    assert len(detail["attempts"]) == 3
    assert all(a["validation_failures"] for a in detail["attempts"])


async def test_a_giving_up_turn_still_records_every_word_the_model_sent(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    """Debugging a model that will not comply is impossible without this."""
    use_llm(FakeLLM(answers=[JUNK, JUNK, JUNK]))
    room_id = await make_room(client, auth)

    turn = await run_turn(client, auth, room_id)
    detail = await inspect(client, auth, turn["id"])

    assert all(a["raw_response"] == JUNK for a in detail["attempts"])
    assert all(a["rendered_prompt"] for a in detail["attempts"])
    assert detail["tokens"]["total"] > 0, "a failed turn still cost money"


# --- network failures -------------------------------------------------------


async def test_a_rate_limit_is_retried_and_the_student_never_notices(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    llm = use_llm(FakeLLM(errors=[ProviderUnavailable("429 rate limited"), None]))
    room_id = await make_room(client, auth)

    turn = await run_turn(client, auth, room_id)

    assert turn["status"] == "succeeded"
    assert len(llm.calls) == 2
    assert llm.prompts[0] == llm.prompts[1], "a retry re-sends the same prompt"


async def test_a_retried_call_is_still_written_down(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    """Why retries live in the turn loop rather than inside a client wrapper.

    Hidden in a wrapper this row would not exist, and a turn that took four
    seconds because of two rate limits would look identical to one that was merely
    slow. The endpoint the brief asks for would be quietly lying.
    """
    use_llm(FakeLLM(errors=[ProviderUnavailable("429 rate limited"), None]))
    room_id = await make_room(client, auth)

    turn = await run_turn(client, auth, room_id)
    first, second = (await inspect(client, auth, turn["id"]))["attempts"]

    assert first["error_type"] == "PROVIDER_UNAVAILABLE"
    assert first["purpose"] == "tutor", "a retry is not a repair"
    assert second["error_type"] is None


async def test_a_provider_that_never_recovers_fails_the_turn(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    llm = use_llm(FakeLLM(error=ProviderUnavailable("still down")))
    room_id = await make_room(client, auth)

    turn = await run_turn(client, auth, room_id)

    assert turn["failure_reason"] == "PROVIDER_UNAVAILABLE"
    assert len(llm.calls) == 3, "one call plus two retries"


async def test_a_safety_refusal_is_not_retried(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    """Retrying nondeterministic failures is sensible; retrying a decision is not.

    The same prompt will be refused again, so a retry buys nothing but a slower
    failure and two more billed calls.
    """
    llm = use_llm(FakeLLM(error=ResponseBlocked("SAFETY")))
    room_id = await make_room(client, auth)

    turn = await run_turn(client, auth, room_id)

    assert turn["failure_reason"] == "RESPONSE_BLOCKED"
    assert len(llm.calls) == 1


async def test_an_empty_response_is_retried(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    """gemini-2.5-flash can spend its whole output budget thinking and return
    nothing. How much it thinks varies run to run, so the next call may well
    succeed — which makes this worth a retry and a safety refusal not."""
    llm = use_llm(FakeLLM(errors=[EmptyResponse("all budget went to thinking"), None]))
    room_id = await make_room(client, auth)

    turn = await run_turn(client, auth, room_id)

    assert turn["status"] == "succeeded"
    assert len(llm.calls) == 2


async def test_the_two_budgets_are_counted_separately(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    """A rate limit and a bad answer are different problems. One shared counter
    would let a single 429 use up the repair budget the turn had not touched."""
    llm = use_llm(
        FakeLLM(answers=[JUNK, GOOD], errors=[ProviderUnavailable("429"), None, None])
    )
    room_id = await make_room(client, auth)

    turn = await run_turn(client, auth, room_id)

    assert turn["status"] == "succeeded"
    assert len(llm.calls) == 3, "one retry then one repair, neither blocking the other"


async def test_every_llm_error_is_a_known_failure_reason() -> None:
    """TurnFailureReason repeats the three LLMError types. This is what stops the
    two copies drifting apart when a new provider error is added."""
    known = {reason.value for reason in TurnFailureReason}
    for error in (ProviderUnavailable("x"), EmptyResponse("x"), ResponseBlocked("x")):
        assert error.error_type in known


# --- staying in the room ----------------------------------------------------


async def test_an_off_topic_question_is_recorded_as_out_of_scope(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    use_llm(
        FakeLLM(
            answers=[
                tutor_json(
                    "This room is for algebra. Try a new room for that topic.",
                    in_scope=False,
                    concepts=[],
                )
            ]
        )
    )
    room_id = await make_room(client, auth)

    turn = await run_turn(client, auth, room_id, "Who won the 2022 world cup?")

    assert turn["status"] == "succeeded", "declining is a correct answer, not a failure"
    assert turn["in_scope"] is False
    assert turn["concepts"] == []


async def test_an_off_topic_turn_is_not_replayed_into_the_next_prompt(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    """Why in_scope is stored rather than merely observed.

    Replaying the football exchange would spend tokens teaching the model to talk
    about football, in a room whose entire purpose is not to.
    """
    off_topic = tutor_json("Wrong room for that.", in_scope=False, concepts=[])
    llm = use_llm(FakeLLM(answers=[off_topic, GOOD]))
    room_id = await make_room(client, auth)

    await run_turn(client, auth, room_id, "Who won the 2022 world cup?")
    await run_turn(client, auth, room_id, "Back to equations please")

    assert "world cup" not in llm.last_prompt.lower()


async def test_an_in_scope_turn_is_replayed(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    """The other half of the rule — filtering must not eat the conversation."""
    llm = use_llm(FakeLLM(answers=[GOOD, GOOD]))
    room_id = await make_room(client, auth)

    await run_turn(client, auth, room_id, "How do I solve 3x + 6 = 15?")
    await run_turn(client, auth, room_id, "Why subtract 6 first?")

    assert "How do I solve 3x + 6 = 15?" in llm.last_prompt


async def test_concepts_are_stored_for_study_history(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    use_llm(FakeLLM(answers=[tutor_json(concepts=["factorising", "common factors"])]))
    room_id = await make_room(client, auth)

    turn = await run_turn(client, auth, room_id)

    assert turn["concepts"] == ["factorising", "common factors"]


# --- the database as the last line of defence -------------------------------


async def assert_rejected_by(db: AsyncSession, constraint: str, row) -> None:
    """Insert one row and require the named constraint to refuse it.

    The insert runs inside a SAVEPOINT. A failed statement poisons the transaction
    it ran in, so without one this would take the test's own transaction down with
    it and the fixture could not roll back cleanly. S6 needs the same helper for
    the quiz constraints.
    """
    try:
        async with db.begin_nested():
            db.add(row)
            await db.flush()
    except IntegrityError as exc:
        assert constraint in str(exc), f"rejected, but not by {constraint}: {exc}"
    else:
        pytest.fail(f"{constraint} did not reject the row")


async def make_room_row(db: AsyncSession, student: Student) -> Room:
    room = Room(student_id=student.id, title="Algebra")
    db.add(room)
    await db.flush()
    return room


async def test_the_database_refuses_an_off_topic_turn_that_claims_to_teach(
    db: AsyncSession, student: Student
) -> None:
    """The last line of defence, below the code that already prevents this.

    `tidy` clears the tags on an out-of-scope turn before anything is saved, so no
    row the pipeline writes can reach here. That is the point of a safety net: it
    is for the row that arrives from a script, or from a future code path that
    forgets. Study history depends on it — an off-topic turn must not contribute
    study time.
    """
    room = await make_room_row(db, student)
    await assert_rejected_by(
        db,
        "off_topic_turns_teach_nothing",
        Turn(
            room_id=room.id,
            seq=1,
            student_message="Who won the world cup?",
            status="succeeded",
            answer_text="Wrong room for that.",
            in_scope=False,
            concepts=["football"],
            completed_at=datetime.now(UTC),
        ),
    )


async def test_the_database_refuses_a_succeeded_turn_with_no_scope_verdict(
    db: AsyncSession, student: Student
) -> None:
    """A succeeded turn knows whether it was in scope. That verdict is part of a
    complete answer, not an optional extra — history queries read it."""
    room = await make_room_row(db, student)
    await assert_rejected_by(
        db,
        "terminal_states_are_complete",
        Turn(
            room_id=room.id,
            seq=1,
            student_message="hi",
            status="succeeded",
            answer_text="hello",
            in_scope=None,
            completed_at=datetime.now(UTC),
        ),
    )
