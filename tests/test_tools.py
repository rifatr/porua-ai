"""The agent loop, and the five protections around it.

The brief's requirement is one sentence — *"Tool use must be controlled; the model
should not call arbitrary code or call tools indefinitely"* — and every test here
is one half of it.

None of these scenarios can be produced by asking the real model nicely. It will
not reliably call a tool that does not exist, pass arguments of the wrong type, or
loop forever on request. That is exactly why `FakeLLM` can script a sequence of
tool requests: without it the protections would be code nobody had ever seen run.
"""

import json

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_llm
from app.config import get_settings
from app.main import app
from app.models.student import Student
from app.tools import registry
from tests.fakes import FakeLLM, tutor_json

settings = get_settings()

CALC = "evaluate_expression"
HISTORY = "query_study_history"
SEARCH = "search_room_materials"


def use_llm(llm: FakeLLM) -> FakeLLM:
    app.dependency_overrides[get_llm] = lambda: llm
    return llm


async def make_room(client: AsyncClient, auth: dict[str, str], title: str = "Algebra") -> str:
    return (await client.post("/rooms", json={"title": title}, headers=auth)).json()["id"]


async def run_turn(
    client: AsyncClient, auth: dict[str, str], room_id: str, message: str = "hi"
) -> dict:
    response = await client.post(
        f"/rooms/{room_id}/turns", json={"message": message}, headers=auth
    )
    assert response.status_code == 201, response.text
    return response.json()


async def inspect(client: AsyncClient, auth: dict[str, str], turn_id: str) -> dict:
    return (await client.get(f"/turns/{turn_id}", headers=auth)).json()


def all_tool_calls(detail: dict) -> list[dict]:
    return [call for attempt in detail["attempts"] for call in attempt["tool_calls"]]


# --- the registry itself ----------------------------------------------------


def test_only_the_declared_tools_exist() -> None:
    """Adding a tool means editing a file, which means it goes through review."""
    assert set(registry.names()) == {HISTORY, CALC, SEARCH}


def test_an_unknown_name_resolves_to_nothing() -> None:
    """Not a check that could be incomplete — there is no mechanism by which an
    unlisted name becomes a callable. No getattr, no import, no dispatch table."""
    for name in ("os.system", "eval", "", "query_study_history "):
        assert registry.resolve(name) is None


def test_declarations_come_from_the_argument_models() -> None:
    """So what the model is told and what is enforced cannot drift apart."""
    spec = next(s for s in registry.specs() if s.name == CALC)
    assert "expression" in spec.parameters["properties"]
    assert spec.description, "the description is the only thing the model reads"


# --- the happy path ---------------------------------------------------------


async def test_a_tool_runs_and_the_turn_answers(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    llm = use_llm(
        FakeLLM(
            tool_calls=[[(CALC, {"expression": "3 * 4 + 2"})], None],
            answers=[tutor_json("It comes to 14.")],
        )
    )
    room_id = await make_room(client, auth)

    turn = await run_turn(client, auth, room_id, "what is 3 * 4 + 2?")

    assert turn["status"] == "succeeded"
    assert turn["answer_text"] == "It comes to 14."
    assert len(llm.calls) == 2, "one call to ask for the tool, one to answer"


async def test_the_tool_result_is_sent_back_to_the_model(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    """Without this the model would ask for the same tool forever."""
    llm = use_llm(
        FakeLLM(tool_calls=[[(CALC, {"expression": "3 * 4 + 2"})], None])
    )
    room_id = await make_room(client, auth)
    await run_turn(client, auth, room_id)

    exchanges = llm.calls[1].tool_exchanges
    assert len(exchanges) == 1
    invocation, result = exchanges[0]
    assert invocation.name == CALC
    assert result.content["result"] == 14


async def test_the_call_is_recorded_with_arguments_and_result(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    """The brief asks the inspection endpoint to show tool activity."""
    use_llm(FakeLLM(tool_calls=[[(CALC, {"expression": "sqrt(144)"})], None]))
    room_id = await make_room(client, auth)

    turn = await run_turn(client, auth, room_id)
    calls = all_tool_calls(await inspect(client, auth, turn["id"]))

    assert len(calls) == 1
    assert calls[0]["tool_name"] == CALC
    assert calls[0]["arguments"] == {"expression": "sqrt(144)"}
    assert calls[0]["result"]["result"] == 12
    assert calls[0]["status"] == "ok"
    assert calls[0]["duration_ms"] is not None


async def test_a_turn_that_needs_no_tool_is_unchanged(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    llm = use_llm(FakeLLM())
    room_id = await make_room(client, auth)

    turn = await run_turn(client, auth, room_id)

    assert turn["status"] == "succeeded"
    assert len(llm.calls) == 1
    assert all_tool_calls(await inspect(client, auth, turn["id"])) == []


# --- protection 1: a fixed list ---------------------------------------------


async def test_an_unknown_tool_is_refused_and_the_turn_still_answers(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    """The model asks for something we never wrote. It gets an error naming what
    does exist, and answers anyway — a refused tool is not a failed turn."""
    use_llm(
        FakeLLM(
            tool_calls=[[("delete_everything", {"target": "/"})], None],
            answers=[tutor_json("Here is the explanation.")],
        )
    )
    room_id = await make_room(client, auth)

    turn = await run_turn(client, auth, room_id)
    calls = all_tool_calls(await inspect(client, auth, turn["id"]))

    assert turn["status"] == "succeeded"
    assert calls[0]["status"] == "unknown_tool"
    assert calls[0]["result"] is None, "nothing ran"
    assert "no tool called 'delete_everything'" in calls[0]["error_detail"]
    assert CALC in calls[0]["error_detail"], "it is told what does exist"


async def test_the_rejected_request_is_still_recorded(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    """Storing only successful calls would hide the protection working."""
    use_llm(FakeLLM(tool_calls=[[("os.system", {"cmd": "rm -rf /"})], None]))
    room_id = await make_room(client, auth)

    turn = await run_turn(client, auth, room_id)
    calls = all_tool_calls(await inspect(client, auth, turn["id"]))

    assert calls[0]["tool_name"] == "os.system"
    assert calls[0]["arguments"] == {"cmd": "rm -rf /"}


# --- protection 2: arguments are checked before anything runs ---------------


async def test_bad_arguments_stop_the_tool_running(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    use_llm(
        FakeLLM(
            tool_calls=[[(HISTORY, {"days_back": "last tuesday"})], None],
            answers=[tutor_json("Let me explain instead.")],
        )
    )
    room_id = await make_room(client, auth)

    turn = await run_turn(client, auth, room_id)
    calls = all_tool_calls(await inspect(client, auth, turn["id"]))

    assert calls[0]["status"] == "invalid_arguments"
    assert calls[0]["result"] is None
    assert "days_back" in calls[0]["error_detail"], "the model is told which field"


async def test_arguments_outside_the_allowed_range_are_refused(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    """The bound is on the model, not just in the description."""
    use_llm(FakeLLM(tool_calls=[[(HISTORY, {"days_back": 99999})], None]))
    room_id = await make_room(client, auth)

    turn = await run_turn(client, auth, room_id)
    calls = all_tool_calls(await inspect(client, auth, turn["id"]))

    assert calls[0]["status"] == "invalid_arguments"


async def test_a_tool_refusing_a_request_is_not_a_crash(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    """The calculator says no to `open(...)`. That is a result, not an error."""
    use_llm(
        FakeLLM(
            tool_calls=[[(CALC, {"expression": "open('/etc/passwd')"})], None],
            answers=[tutor_json("I cannot compute that.")],
        )
    )
    room_id = await make_room(client, auth)

    turn = await run_turn(client, auth, room_id)
    calls = all_tool_calls(await inspect(client, auth, turn["id"]))

    assert turn["status"] == "succeeded"
    assert calls[0]["status"] == "failed"
    assert "Unknown function 'open'" in calls[0]["error_detail"]


# --- protection 3: identity never comes from the model ----------------------


def test_no_tool_accepts_a_student_id() -> None:
    """The strongest form of this guarantee: the parameter does not exist.

    A check that rejects a model-supplied student id can be forgotten on the next
    tool. A tool signature with nowhere to put one cannot be.
    """
    for spec in registry.specs():
        fields = set(spec.parameters.get("properties", {}))
        assert not {"student_id", "user_id", "room_id"} & fields, spec.name


async def test_a_tool_reads_only_the_caller_own_history(
    client: AsyncClient, db: AsyncSession, auth: dict[str, str], student: Student
) -> None:
    other = Student(display_name="Someone Else", education_level="Class 9")
    db.add(other)
    await db.flush()
    other_auth = {"X-Student-Id": str(other.id)}

    # The other student studies something distinctive.
    use_llm(FakeLLM(answers=[tutor_json(concepts=["photosynthesis"])]))
    their_room = await make_room(client, other_auth, "Biology")
    await run_turn(client, other_auth, their_room)

    # Our student asks what they have studied.
    llm = use_llm(FakeLLM(tool_calls=[[(HISTORY, {"days_back": 30})], None]))
    my_room = await make_room(client, auth, "Algebra")
    await run_turn(client, auth, my_room, "what have I studied?")

    _, result = llm.calls[1].tool_exchanges[0]
    concepts = [item["concept"] for item in result.content["concepts"]]
    assert "photosynthesis" not in concepts
    assert "Biology" not in [room["title"] for room in result.content["rooms"]]


# --- protection 4: hard limits ----------------------------------------------


async def test_a_model_that_never_stops_calling_tools_is_stopped(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    """The runaway-loop test. A model that asks for a tool every single time.

    The loop does not fail the turn — when the iteration budget runs out the tools
    stop being *offered*, so the model has to answer with what it has. A student
    waiting gets an answer rather than an error, and the loop still terminates.
    """
    llm = use_llm(
        FakeLLM(
            tool_calls=[[(CALC, {"expression": f"{n} + 1"})] for n in range(20)],
            answers=[tutor_json("Right, here is the answer.")],
        )
    )
    room_id = await make_room(client, auth)

    turn = await run_turn(client, auth, room_id)
    detail = await inspect(client, auth, turn["id"])

    assert turn["status"] == "succeeded"
    assert len(all_tool_calls(detail)) == settings.max_agent_iterations
    # One model call per tool round, plus the final one that had no tools offered.
    assert len(llm.calls) == settings.max_agent_iterations + 1
    assert llm.calls[-1].tools == (), "the last call offered no tools, forcing an answer"


async def test_the_per_turn_tool_budget_is_enforced(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    """Four tools per round runs past the six-call budget inside two rounds."""
    batch = [(CALC, {"expression": f"{n} + 1"}) for n in range(4)]
    use_llm(
        FakeLLM(
            tool_calls=[batch, batch, batch],
            answers=[tutor_json("Answering now.")],
        )
    )
    room_id = await make_room(client, auth)

    turn = await run_turn(client, auth, room_id)
    calls = all_tool_calls(await inspect(client, auth, turn["id"]))

    ran = [call for call in calls if call["status"] == "ok"]
    refused = [call for call in calls if call["status"] == "failed"]

    assert turn["status"] == "succeeded"
    assert len(ran) == settings.max_tool_calls_per_turn
    assert refused, "the calls past the budget were refused, not silently dropped"
    assert "budget" in refused[0]["error_detail"].lower()


async def test_every_request_gets_a_response_even_when_refused(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    """Function calling is a conversation. An unanswered call makes the next
    request malformed, so 'no' has to be said out loud."""
    llm = use_llm(
        FakeLLM(
            tool_calls=[[("nope", {}), (CALC, {"expression": "1+1"})], None],
        )
    )
    room_id = await make_room(client, auth)
    await run_turn(client, auth, room_id)

    assert len(llm.calls[1].tool_exchanges) == 2


# --- protection 5: the same call twice --------------------------------------


async def test_a_repeated_call_is_answered_from_cache(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    """Limits end a loop eventually. This ends it now.

    The second result carries a note telling the model it already asked, so it
    stops rather than varying the arguments slightly and trying again.
    """
    same = [(CALC, {"expression": "2 + 2"})]
    use_llm(
        FakeLLM(tool_calls=[same, same, None], answers=[tutor_json("It is 4.")])
    )
    room_id = await make_room(client, auth)

    turn = await run_turn(client, auth, room_id)
    calls = all_tool_calls(await inspect(client, auth, turn["id"]))

    assert len(calls) == 2
    assert calls[1]["status"] == "ok"
    assert "already called this" in calls[1]["result"]["note"]
    assert calls[1]["result"]["result"] == 4, "and the same answer comes back"


async def test_argument_order_does_not_defeat_the_cache(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    """The model can emit the same two arguments in either order."""
    from app.llm.base import ToolInvocation
    from app.services.turn import _cache_key

    first = ToolInvocation(name=HISTORY, arguments={"a": 1, "b": 2})
    second = ToolInvocation(name=HISTORY, arguments={"b": 2, "a": 1})
    assert _cache_key(first) == _cache_key(second)


# --- the provider constraint that shapes all of this ------------------------


async def test_tools_and_response_schema_are_never_sent_together(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    """Gemini answers a request carrying both with 400 "Function calling with a
    response mime type: 'application/json' is unsupported".

    So constrained decoding is unavailable exactly while tools are in play, and
    the parse/schema/content pipeline is the only thing checking that output.
    """
    llm = use_llm(FakeLLM(tool_calls=[[(CALC, {"expression": "1+1"})], None]))
    room_id = await make_room(client, auth)
    await run_turn(client, auth, room_id)

    for call in llm.calls:
        assert not (call.tools and call.response_schema is not None)


async def test_a_repair_call_offers_no_tools(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    """A repair is a reformatting job, so it gets constrained decoding back."""
    llm = use_llm(FakeLLM(answers=["this is not json at all", tutor_json()]))
    room_id = await make_room(client, auth)

    turn = await run_turn(client, auth, room_id)

    assert turn["status"] == "succeeded"
    assert llm.calls[1].tools == ()
    assert llm.calls[1].response_schema is not None


# --- what the inspection endpoint shows -------------------------------------


async def test_tool_calls_hang_off_the_attempt_that_asked_for_them(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    """So the turn reads as a story rather than a flat list of calls."""
    use_llm(
        FakeLLM(
            tool_calls=[
                [(CALC, {"expression": "1+1"})],
                [(HISTORY, {"days_back": 7})],
                None,
            ]
        )
    )
    room_id = await make_room(client, auth)

    turn = await run_turn(client, auth, room_id)
    detail = await inspect(client, auth, turn["id"])

    assert [len(a["tool_calls"]) for a in detail["attempts"]] == [1, 1, 0]
    assert detail["attempts"][0]["tool_calls"][0]["tool_name"] == CALC
    assert detail["attempts"][1]["tool_calls"][0]["tool_name"] == HISTORY


async def test_the_timeline_still_omits_tool_detail(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    """Tool results can be large. The conversation view must stay small."""
    use_llm(FakeLLM(tool_calls=[[(HISTORY, {"days_back": 7})], None]))
    room_id = await make_room(client, auth)
    await run_turn(client, auth, room_id)

    item = (await client.get(f"/rooms/{room_id}/turns", headers=auth)).json()["items"][0]
    assert "attempts" not in item
    assert "tool_calls" not in item


async def test_arguments_are_stored_as_json_not_a_string(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    """So "how often did the model pass a bad days_back" is a query, not a grep."""
    use_llm(FakeLLM(tool_calls=[[(HISTORY, {"days_back": 7})], None]))
    room_id = await make_room(client, auth)

    turn = await run_turn(client, auth, room_id)
    calls = all_tool_calls(await inspect(client, auth, turn["id"]))

    assert calls[0]["arguments"] == {"days_back": 7}
    assert json.dumps(calls[0]["arguments"])
