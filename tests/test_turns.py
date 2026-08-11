"""Turns, and the inspection endpoint.

The point of S2 is bookkeeping, not answer quality: every call to the model must
leave a record, especially when it fails. These tests check that record exists and
is complete, because S3's whole reliability story reads from it.
"""

from uuid import UUID

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.base import EmptyResponse, ProviderUnavailable
from app.models.student import Student
from app.models.turn import Turn
from tests.fakes import FakeLLM


async def make_room(client: AsyncClient, auth: dict[str, str], title: str = "Algebra") -> str:
    response = await client.post("/rooms", json={"title": title}, headers=auth)
    return response.json()["id"]


# --- the happy path ---------------------------------------------------------


async def test_a_turn_saves_the_answer(
    client: AsyncClient, auth: dict[str, str], fake_llm: FakeLLM
) -> None:
    room_id = await make_room(client, auth)
    response = await client.post(
        f"/rooms/{room_id}/turns", json={"message": "How do I solve 3x + 6 = 15?"}, headers=auth
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "succeeded"
    assert body["seq"] == 1
    assert body["answer_text"] == "Subtract 6 from both sides, then divide by 3. x = 3."
    assert body["failure_reason"] is None
    assert len(fake_llm.calls) == 1


async def test_turn_numbers_increase_within_a_room(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    room_id = await make_room(client, auth)
    for expected_seq in (1, 2, 3):
        response = await client.post(
            f"/rooms/{room_id}/turns", json={"message": "again"}, headers=auth
        )
        assert response.json()["seq"] == expected_seq


async def test_a_turn_bumps_the_room_to_the_top_of_the_list(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    first = await make_room(client, auth, "First room")
    await make_room(client, auth, "Second room")

    await client.post(f"/rooms/{first}/turns", json={"message": "hello"}, headers=auth)

    rooms = (await client.get("/rooms", headers=auth)).json()["items"]
    assert rooms[0]["title"] == "First room"


# --- what reaches the model -------------------------------------------------


async def test_the_prompt_carries_the_education_level_and_topic(
    client: AsyncClient, auth: dict[str, str], student: Student, fake_llm: FakeLLM
) -> None:
    """Grade-appropriateness depends on this actually arriving, not just being in the template."""
    room_id = await make_room(client, auth, "AP Biology: Cell Respiration")
    await client.post(f"/rooms/{room_id}/turns", json={"message": "explain ATP"}, headers=auth)

    prompt = fake_llm.last_prompt
    assert student.education_level in prompt
    assert "AP Biology: Cell Respiration" in prompt
    assert "explain ATP" in prompt


async def test_earlier_turns_are_replayed_into_the_next_prompt(
    client: AsyncClient, auth: dict[str, str], fake_llm: FakeLLM
) -> None:
    """This is what makes a room a conversation rather than a series of strangers."""
    room_id = await make_room(client, auth)

    await client.post(
        f"/rooms/{room_id}/turns", json={"message": "How do I solve 3x + 6 = 15?"}, headers=auth
    )
    await client.post(
        f"/rooms/{room_id}/turns", json={"message": "Why subtract 6 first?"}, headers=auth
    )

    second_prompt = fake_llm.last_prompt
    assert "How do I solve 3x + 6 = 15?" in second_prompt
    assert "Subtract 6 from both sides" in second_prompt


async def test_the_first_prompt_has_no_earlier_conversation_block(
    client: AsyncClient, auth: dict[str, str], fake_llm: FakeLLM
) -> None:
    room_id = await make_room(client, auth)
    await client.post(f"/rooms/{room_id}/turns", json={"message": "first"}, headers=auth)
    assert "## Earlier in this room" not in fake_llm.last_prompt


async def test_a_failed_turn_is_not_replayed_as_context(
    client: AsyncClient, auth: dict[str, str], db: AsyncSession, student: Student
) -> None:
    """A failure has no answer, so feeding it back would put a hole in the history."""
    room_id = await make_room(client, auth)

    broken = FakeLLM(error=ProviderUnavailable("boom"))
    from app.api.deps import get_llm
    from app.main import app

    app.dependency_overrides[get_llm] = lambda: broken
    await client.post(f"/rooms/{room_id}/turns", json={"message": "doomed"}, headers=auth)

    working = FakeLLM()
    app.dependency_overrides[get_llm] = lambda: working
    await client.post(f"/rooms/{room_id}/turns", json={"message": "next"}, headers=auth)

    assert "doomed" not in working.last_prompt


# --- failures ---------------------------------------------------------------


async def test_a_provider_failure_produces_a_typed_failure_not_a_500(
    client: AsyncClient, auth: dict[str, str], db: AsyncSession
) -> None:
    room_id = await make_room(client, auth)

    from app.api.deps import get_llm
    from app.main import app

    app.dependency_overrides[get_llm] = lambda: FakeLLM(error=ProviderUnavailable("429"))
    response = await client.post(
        f"/rooms/{room_id}/turns", json={"message": "hello"}, headers=auth
    )

    assert response.status_code == 201, "a failed turn is still a recorded turn"
    body = response.json()
    assert body["status"] == "failed"
    assert body["failure_reason"] == "PROVIDER_UNAVAILABLE"
    assert body["answer_text"] is None, "a failed turn must never carry an answer"


async def test_an_empty_response_is_recorded_with_its_reason(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    """gemini-2.5-flash can spend its whole output budget thinking and return nothing."""
    room_id = await make_room(client, auth)

    from app.api.deps import get_llm
    from app.main import app

    app.dependency_overrides[get_llm] = lambda: FakeLLM(
        error=EmptyResponse("all budget went to thinking")
    )
    turn = (
        await client.post(f"/rooms/{room_id}/turns", json={"message": "hi"}, headers=auth)
    ).json()

    assert turn["failure_reason"] == "EMPTY_RESPONSE"

    detail = (await client.get(f"/turns/{turn['id']}", headers=auth)).json()
    assert detail["attempts"][0]["error_type"] == "EMPTY_RESPONSE"
    assert "thinking" in detail["attempts"][0]["error_detail"]


# --- the inspection endpoint ------------------------------------------------


async def test_inspection_shows_the_prompt_its_version_and_checksum(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    room_id = await make_room(client, auth)
    turn_id = (
        await client.post(f"/rooms/{room_id}/turns", json={"message": "hi"}, headers=auth)
    ).json()["id"]

    detail = (await client.get(f"/turns/{turn_id}", headers=auth)).json()

    assert len(detail["attempts"]) == 1
    attempt = detail["attempts"][0]
    assert attempt["prompt_name"] == "tutor_system"
    assert attempt["prompt_version"] == 1
    assert len(attempt["prompt_sha256"]) == 64
    assert "hi" in attempt["rendered_prompt"]
    assert attempt["request_params"]["model"]
    assert attempt["finish_reason"] == "STOP"
    assert attempt["duration_ms"] is not None


async def test_inspection_reports_token_usage_including_thinking(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    """Thinking tokens are billed but never visible in the answer, so they are counted apart."""
    room_id = await make_room(client, auth)
    turn_id = (
        await client.post(f"/rooms/{room_id}/turns", json={"message": "hi"}, headers=auth)
    ).json()["id"]

    tokens = (await client.get(f"/turns/{turn_id}", headers=auth)).json()["tokens"]

    assert tokens == {"prompt": 100, "output": 40, "thought": 250, "total": 390}


async def test_a_failed_attempt_is_still_recorded(
    client: AsyncClient, auth: dict[str, str], db: AsyncSession
) -> None:
    """If a failure skipped persistence, the inspection endpoint would be lying."""
    room_id = await make_room(client, auth)

    from app.api.deps import get_llm
    from app.main import app

    app.dependency_overrides[get_llm] = lambda: FakeLLM(error=ProviderUnavailable("down"))
    turn_id = (
        await client.post(f"/rooms/{room_id}/turns", json={"message": "hi"}, headers=auth)
    ).json()["id"]

    detail = (await client.get(f"/turns/{turn_id}", headers=auth)).json()

    assert len(detail["attempts"]) == 1
    attempt = detail["attempts"][0]
    assert attempt["error_type"] == "PROVIDER_UNAVAILABLE"
    assert attempt["raw_response"] is None
    assert attempt["rendered_prompt"], "we must still know what we sent"


async def test_another_students_turn_is_not_found(
    client: AsyncClient, db: AsyncSession, auth: dict[str, str]
) -> None:
    other = Student(display_name="Someone Else", education_level="Class 9")
    db.add(other)
    await db.flush()

    other_auth = {"X-Student-Id": str(other.id)}
    room_id = await make_room(client, other_auth, "Their room")
    turn_id = (
        await client.post(
            f"/rooms/{room_id}/turns", json={"message": "private"}, headers=other_auth
        )
    ).json()["id"]

    assert (await client.get(f"/turns/{turn_id}", headers=auth)).status_code == 404


async def test_cannot_post_a_turn_into_another_students_room(
    client: AsyncClient, db: AsyncSession, auth: dict[str, str]
) -> None:
    other = Student(display_name="Someone Else", education_level="Class 9")
    db.add(other)
    await db.flush()
    room_id = await make_room(client, {"X-Student-Id": str(other.id)}, "Their room")

    response = await client.post(
        f"/rooms/{room_id}/turns", json={"message": "sneaky"}, headers=auth
    )
    assert response.status_code == 404

    # Scoped to this room, not the whole table: asserting on global state would
    # also pick up rows committed outside the test.
    in_that_room = (
        (await db.execute(select(Turn).where(Turn.room_id == UUID(room_id))))
        .scalars()
        .all()
    )
    assert in_that_room == [], "the request must not have created a turn"


# --- listing ----------------------------------------------------------------


async def test_room_timeline_is_newest_first_and_paged(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    room_id = await make_room(client, auth)
    for n in range(3):
        await client.post(
            f"/rooms/{room_id}/turns", json={"message": f"question {n}"}, headers=auth
        )

    page = (await client.get(f"/rooms/{room_id}/turns?limit=2", headers=auth)).json()

    assert [t["seq"] for t in page["items"]] == [3, 2]
    assert page["next_cursor"] is not None

    rest = (
        await client.get(
            f"/rooms/{room_id}/turns?limit=2&cursor={page['next_cursor']}", headers=auth
        )
    ).json()
    assert [t["seq"] for t in rest["items"]] == [1]
    assert rest["next_cursor"] is None


async def test_the_timeline_omits_prompts_and_raw_responses(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    """They can be tens of kilobytes each — the list view must stay small."""
    room_id = await make_room(client, auth)
    await client.post(f"/rooms/{room_id}/turns", json={"message": "hi"}, headers=auth)

    item = (await client.get(f"/rooms/{room_id}/turns", headers=auth)).json()["items"][0]

    assert "rendered_prompt" not in item
    assert "attempts" not in item


async def test_a_blank_message_is_rejected(client: AsyncClient, auth: dict[str, str]) -> None:
    room_id = await make_room(client, auth)
    response = await client.post(f"/rooms/{room_id}/turns", json={"message": ""}, headers=auth)
    assert response.status_code == 422
