"""Skills — the checks, not the prompts.

The brief's bar is that a skill must not be "a renamed version of the same
prompt". These tests are the evidence for that claim: almost none of them care
what the model said, and almost all of them care what our code did about it.

Every quiz test drives a *deliberately broken* model reply through `FakeLLM` and
asserts we caught it — which is not something a real model can be asked to do on
demand.
"""

import json
import time

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_llm
from app.llm import EmptyResponse, ProviderUnavailable, ResponseBlocked
from app.main import app
from app.models.room import Room
from app.models.skill_run import CHOICES_PER_QUESTION
from app.models.student import Student
from app.skills import registry
from app.skills.quiz_builder import DraftQuiz, QuizArgs, balance_positions, check_quiz
from app.skills.step_solver import DraftSolution, check_solution, verified_steps
from app.skills.verify import (
    MAX_POLYNOMIAL_DEGREE,
    UnparseableStep,
    answer_satisfies,
    parse_equation,
    steps_are_equivalent,
)
from tests.fakes import FakeLLM

QUIZ = "quiz_builder"
SOLVER = "step_solver"


def use_llm(llm: FakeLLM) -> FakeLLM:
    """Swap the model for this test. The client fixture clears it afterwards."""
    app.dependency_overrides[get_llm] = lambda: llm
    return llm


async def add_room(db: AsyncSession, student: Student, title: str = "Biology") -> Room:
    room = Room(student_id=student.id, title=title)
    db.add(room)
    await db.flush()
    return room


def question(
    stem: str = "Q",
    correct: str = "right",
    wrong: tuple[str, ...] = ("a", "b", "c"),
):
    return {
        "stem": stem,
        "choices": [{"text": correct, "is_correct": True}]
        + [{"text": text, "is_correct": False} for text in wrong],
        "explanation": "because",
    }


def quiz(*questions) -> DraftQuiz:
    return DraftQuiz.model_validate({"questions": list(questions)})


# --- the registry -----------------------------------------------------------


def test_only_the_declared_skills_exist() -> None:
    """Adding a skill means editing a file, which means it goes through review."""
    assert set(registry.names()) == {QUIZ, SOLVER}


def test_an_unknown_skill_name_resolves_to_nothing() -> None:
    for name in ("os.system", "", "quiz_builder "):
        assert registry.resolve(name) is None


# --- the four quiz failures the brief names ---------------------------------


def test_a_duplicate_option_is_caught() -> None:
    """The brief's first named failure. Normalised, so "Paris" and " paris "
    are one answer — which is how a student reads them."""
    problems = check_quiz(
        quiz(question(correct="Paris", wrong=(" paris ", "Rome", "Madrid"))), expected=1
    )
    assert any("written twice" in problem for problem in problems)


def test_the_wrong_number_of_questions_is_caught() -> None:
    problems = check_quiz(quiz(question(), question()), expected=5)
    assert any("exactly 5 were asked for" in problem for problem in problems)


def test_the_wrong_number_of_options_is_caught() -> None:
    problems = check_quiz(quiz(question(wrong=("a", "b"))), expected=1)
    assert any(f"exactly {CHOICES_PER_QUESTION}" in problem for problem in problems)


def test_no_correct_answer_is_caught() -> None:
    bad = question()
    bad["choices"][0]["is_correct"] = False
    problems = check_quiz(quiz(bad), expected=1)
    assert any("0 options marked correct" in problem for problem in problems)


def test_two_correct_answers_are_caught() -> None:
    bad = question()
    bad["choices"][1]["is_correct"] = True
    problems = check_quiz(quiz(bad), expected=1)
    assert any("2 options marked correct" in problem for problem in problems)


def test_a_blank_option_is_caught() -> None:
    bad = question()
    bad["choices"][2]["text"] = "   "
    assert any("blank" in problem for problem in check_quiz(quiz(bad), expected=1))


def test_a_good_quiz_has_no_problems() -> None:
    assert check_quiz(quiz(question(), question()), expected=2) == []


def test_every_problem_is_reported_not_just_the_first() -> None:
    """One repair call fixing four faults beats four calls fixing one each."""
    bad = question(correct="same", wrong=("same", "b"))
    bad["explanation"] = ""
    problems = check_quiz(quiz(bad), expected=3)
    assert len(problems) >= 4


# --- the failure that cannot be a check -------------------------------------


def test_shuffling_moves_the_answer_off_the_model_s_chosen_slot() -> None:
    """The brief's fourth failure: predictable answer positions.

    Not a check, because no single quiz is wrong — it is a bias across many. It
    is fixed by construction instead, which is why this asserts on the *spread*
    rather than on any one question.
    """
    # A model doing the thing models do: correct answer first, every time.
    drafted = quiz(*[question(stem=f"Q{index}") for index in range(20)])
    assert all(q.choices[0].is_correct for q in drafted.questions)

    shuffled = balance_positions(drafted, seed=7)

    positions = {
        next(i for i, c in enumerate(q.choices) if c.is_correct)
        for q in shuffled.questions
    }
    assert len(positions) == CHOICES_PER_QUESTION, "the answer never left one slot"


def test_shuffling_keeps_every_option_and_exactly_one_answer() -> None:
    original = quiz(question(correct="right", wrong=("a", "b", "c")))
    shuffled = balance_positions(original, seed=3)

    before = {c.text for c in original.questions[0].choices}
    after = {c.text for c in shuffled.questions[0].choices}
    assert before == after
    assert sum(c.is_correct for c in shuffled.questions[0].choices) == 1


def test_the_same_request_shuffles_the_same_way() -> None:
    """Seeded from the request, so a test can assert on the result and a student
    re-reading a quiz sees the options where they left them."""
    from app.skills.quiz_builder import _seed

    first = _seed("photosynthesis", QuizArgs(question_count=5))
    second = _seed("photosynthesis", QuizArgs(question_count=5))
    assert first == second
    assert _seed("mitosis", QuizArgs(question_count=5)) != first


# --- algebra, checked -------------------------------------------------------


@pytest.mark.parametrize(
    ("before", "after", "ok"),
    [
        ("3x + 6 = 15", "3x = 9", True),
        ("3x + 6 = 15", "3x = 8", False),  # arithmetic slip
        ("3x = 9", "x = 3", True),
        ("3x = 9", "x = 4", False),
        ("2(x - 4) = 10", "2x - 8 = 10", True),
        ("2(x - 4) = 10", "2x - 4 = 10", False),  # forgot to distribute
        ("x^2 - 4 = 0", "x = 2", False),  # dropped a root
    ],
)
def test_a_step_is_accepted_only_when_it_keeps_the_same_solutions(
    before: str, after: str, ok: bool
) -> None:
    """The rule that makes this a solver rather than a prompt. It knows nothing
    about which operation was claimed — a step is accepted because it preserves
    the answer, which is what makes it hard to talk past."""
    assert steps_are_equivalent(before, after).ok is ok


def test_the_final_answer_is_substituted_back() -> None:
    assert answer_satisfies("3x + 6 = 15", "x = 3").ok
    assert not answer_satisfies("3x + 6 = 15", "x = 4").ok


def test_working_with_a_wrong_middle_step_is_rejected() -> None:
    solution = DraftSolution.model_validate(
        {
            "steps": [
                {"expression": "3x = 8", "explanation": "subtract 6"},
                {"expression": "x = 3", "explanation": "divide by 3"},
            ],
            "answer": "x = 3",
        }
    )
    problems = check_solution(solution, problem="3x + 6 = 15")
    assert any("step 1 is wrong" in problem for problem in problems)


def test_correct_working_passes() -> None:
    solution = DraftSolution.model_validate(
        {
            "steps": [
                {"expression": "3x = 9", "explanation": "subtract 6 from both sides"},
                {"expression": "x = 3", "explanation": "divide both sides by 3"},
            ],
            "answer": "x = 3",
        }
    )
    assert check_solution(solution, problem="3x + 6 = 15") == []


@pytest.mark.parametrize(
    "hostile",
    ['__import__("os").system("ls")', "x.__class__.__bases__", 'open("/etc/passwd")'],
)
def test_the_checker_never_hands_arbitrary_text_to_sympy(hostile: str) -> None:
    """`sympify` runs Python and has had sandbox escapes. The string here comes
    from a student by way of a language model, so it is untrusted twice — the
    allow-list runs first, exactly as it does in `evaluate_expression`."""
    with pytest.raises(UnparseableStep):
        parse_equation(hostile)


@pytest.mark.parametrize(
    "bomb",
    [
        "9^9^9 = 1",  # six characters; a 370-million-digit integer if evaluated
        "2^x^x = 1",  # an exponent that cannot be measured without running it
        "2^2000 = 1",  # a number too large to be worth computing
        "x^99999 = 1",  # parses instantly, then never leaves solveset
    ],
)
def test_a_power_that_would_never_finish_is_refused(bomb: str) -> None:
    """The allow-list stops code, not cost, and a hang is the same outcome as an
    escape for an API someone is waiting on. Every one of these passes the
    character test, so the guard has to be about the work, not the spelling."""
    started = time.monotonic()
    with pytest.raises(UnparseableStep):
        parse_equation(bomb)
    assert time.monotonic() - started < 1, "the guard must refuse without computing"


@pytest.mark.parametrize("degree", [2, 3, 4, MAX_POLYNOMIAL_DEGREE])
def test_the_powers_school_algebra_actually_uses_still_parse(degree: int) -> None:
    assert parse_equation(f"x^{degree} - 1 = 0") is not None


@pytest.mark.parametrize(
    "problem, answer",
    [
        ("2x = 4", "2 = 2"),  # an identity, not a value
        ("2x = 4", "x = x"),  # also collapses to a boolean
    ],
)
def test_an_answer_with_no_sides_is_a_verdict_not_a_crash(
    problem: str, answer: str
) -> None:
    """sympy evaluates `Eq(2, 2)` to a plain `True`, which has no `lhs` to read.
    Reaching for it was an AttributeError — a 500 from the one module whose whole
    job is to fail in the open."""
    outcome = answer_satisfies(problem, answer)
    assert outcome.checkable is False
    assert steps_are_equivalent("6 = 6", "x + y = 2").checkable is False


def test_an_unverifiable_step_is_not_reported_as_a_wrong_one() -> None:
    """"We could not check this" and "we checked it and it is wrong" are
    different things to tell a student, and `checkable` is what keeps them apart
    — it used to be inferred by substring-matching the detail message."""
    solution = DraftSolution.model_validate(
        {
            "steps": [{"expression": "3x = 9", "explanation": "subtract 6"}],
            "answer": "x = x",  # no verdict available
        }
    )
    assert check_solution(solution, problem="3x + 6 = 15") == []
    steps = verified_steps(solution, problem="3x + 6 = 15")
    assert steps[0]["verified"] is True and steps[0]["checkable"] is True


# --- running a skill end to end ---------------------------------------------


async def run_skill(
    client: AsyncClient, auth: dict[str, str], room: Room, name: str, body: dict
) -> tuple[int, dict]:
    response = await client.post(
        f"/rooms/{room.id}/skills/{name}/runs", headers=auth, json=body
    )
    return response.status_code, response.json()


async def test_a_quiz_is_built_stored_and_shuffled(
    client: AsyncClient,
    auth: dict[str, str],
    db: AsyncSession,
    student: Student,
    fake_llm: FakeLLM,
) -> None:
    room = await add_room(db, student)
    quiz_json = json.dumps({"questions": [question(stem=f"Q{i}") for i in range(3)]})
    use_llm(FakeLLM(answers=[quiz_json]))

    status, body = await run_skill(client, auth, room, QUIZ, {"question_count": 3})

    assert status == 201, body
    assert body["status"] == "succeeded"
    assert len(body["questions"]) == 3
    for stored in body["questions"]:
        assert len(stored["choices"]) == CHOICES_PER_QUESTION
        assert sum(choice["is_correct"] for choice in stored["choices"]) == 1
        # Contiguous and in order, so a client can render the labels itself.
        positions = [choice["position"] for choice in stored["choices"]]
        assert positions == list(range(CHOICES_PER_QUESTION))


async def test_a_broken_quiz_is_repaired_rather_than_served(
    client: AsyncClient,
    auth: dict[str, str],
    db: AsyncSession,
    student: Student,
    fake_llm: FakeLLM,
) -> None:
    """The whole point of the slice: the first reply has a duplicate option, and
    the student never sees it."""
    room = await add_room(db, student)
    broken = [question(correct="Paris", wrong=("paris", "Rome", "Madrid"))] + [
        question(stem="Q2"),
        question(stem="Q3"),
    ]
    fixed = [question(correct="Paris", wrong=("Lyon", "Rome", "Madrid"))] + [
        question(stem="Q2"),
        question(stem="Q3"),
    ]
    fake_llm = use_llm(
        FakeLLM(
            answers=[
                json.dumps({"questions": broken}),
                json.dumps({"questions": fixed}),
            ]
        )
    )

    status, body = await run_skill(client, auth, room, QUIZ, {"question_count": 3})

    assert status == 201, body
    assert body["status"] == "succeeded"
    assert len(fake_llm.calls) == 2, "the bad quiz should have been sent back"
    # The retry names the fault, and only the fault.
    assert "written twice" in fake_llm.calls[1].prompt
    texts = {choice["text"] for choice in body["questions"][0]["choices"]}
    assert "paris" not in texts


async def test_a_quiz_that_never_passes_fails_loudly(
    client: AsyncClient,
    auth: dict[str, str],
    db: AsyncSession,
    student: Student,
    fake_llm: FakeLLM,
) -> None:
    """A quiz with two identical options is worse than no quiz, so the run fails
    rather than degrading — and the row keeps the evidence."""
    room = await add_room(db, student)
    broken = json.dumps(
        {
            "questions": [
                question(correct="Paris", wrong=("paris", "a", "b")),
                question(stem="Q2"),
                question(stem="Q3"),
            ]
        }
    )
    use_llm(FakeLLM(answers=[broken, broken, broken, broken]))

    status, body = await run_skill(client, auth, room, QUIZ, {"question_count": 3})

    assert status == 201
    assert body["status"] == "failed"
    assert body["failure_reason"] == "CHECKS_FAILED"
    assert body["questions"] == []
    assert len(body["checks"]) >= 3, "every attempt's checks should be kept"


async def test_the_solver_records_which_steps_were_verified(
    client: AsyncClient,
    auth: dict[str, str],
    db: AsyncSession,
    student: Student,
    fake_llm: FakeLLM,
) -> None:
    """`We verify the maths` is a claim; this is where the evidence is kept."""
    room = await add_room(db, student, "Algebra")
    use_llm(FakeLLM(answers=[
            json.dumps(
                {
                    "steps": [
                        {"expression": "3x = 9", "explanation": "subtract 6"},
                        {"expression": "x = 3", "explanation": "divide by 3"},
                    ],
                    "answer": "x = 3",
                }
            )
        ]))

    status, body = await run_skill(client, auth, room, SOLVER, {"problem": "3x + 6 = 15"})

    assert status == 201, body
    assert body["status"] == "succeeded"
    assert body["result"]["answer"] == "x = 3"
    verified = body["checks"][-1]["verified_steps"]
    assert [step["verified"] for step in verified] == [True, True]


async def test_the_solver_declines_a_problem_it_cannot_check(
    client: AsyncClient,
    auth: dict[str, str],
    db: AsyncSession,
    student: Student,
    fake_llm: FakeLLM,
) -> None:
    """Without its checks this skill is the renamed prompt the brief warns
    about, so it refuses rather than pretending — before spending a model call."""
    room = await add_room(db, student, "Algebra")

    status, body = await run_skill(
        client, auth, room, SOLVER, {"problem": "if Ali has 3 apples and eats one"}
    )

    assert status == 201
    assert body["failure_reason"] == "UNCHECKABLE_PROBLEM"
    assert fake_llm.calls == [], "no model call should have been made"


async def test_prose_that_happens_to_parse_is_still_refused(
    client: AsyncClient,
    auth: dict[str, str],
    db: AsyncSession,
    student: Student,
    fake_llm: FakeLLM,
) -> None:
    """The allow-list decides whether sympy can *read* a string, which is not the
    same question as whether it is maths. "Give me a quiz on TCP" is letters and
    spaces, so implicit multiplication turns it into a product of fourteen
    symbols — a valid expression nobody asked to solve. Symbol count is what
    separates it from `3x + 6 = 15`."""
    room = await add_room(db, student, "Algebra")

    status, body = await run_skill(
        client, auth, room, SOLVER, {"problem": "Give me a quiz on TCP"}
    )

    assert status == 201
    assert body["failure_reason"] == "UNCHECKABLE_PROBLEM"
    assert "unknowns" in body["failure_detail"]
    # The point of refusing at the gate: this used to cost three model calls and
    # then fail as CHECKS_FAILED, which is the wrong reason as well as the slow one.
    assert fake_llm.calls == []


# --- the reliability layers a skill inherits --------------------------------


async def test_a_transient_provider_error_is_retried_not_fatal(
    client: AsyncClient,
    auth: dict[str, str],
    db: AsyncSession,
    student: Student,
) -> None:
    """An empty reply is the likeliest real failure on gemini-2.5-flash — it
    means the model spent its whole output budget thinking. The tutor has always
    retried it; skills used to die on the first one, so the same rate limit cost
    the tutor half a second and a quiz the entire run."""
    room = await add_room(db, student)
    good = json.dumps({"questions": [question(stem=f"Q{i}") for i in range(3)]})
    use_llm(
        FakeLLM(
            answers=[good],
            errors=[EmptyResponse("thought itself out of budget"), None],
        )
    )

    status, body = await run_skill(client, auth, room, QUIZ, {"question_count": 3})

    assert status == 201
    assert body["status"] == "succeeded"

    # The failed call is still a row. A retry hidden inside a wrapper would make
    # the turn look cheaper than it was.
    detail = (await client.get(f"/turns/{body['turn_id']}", headers=auth)).json()
    assert [a["error_type"] for a in detail["attempts"]] == ["EMPTY_RESPONSE", None]


async def test_a_retry_does_not_spend_the_repair_budget(
    client: AsyncClient,
    auth: dict[str, str],
    db: AsyncSession,
    student: Student,
) -> None:
    """Three rate limits and three bad answers are different problems. Sharing
    one counter would mean a flaky network ate the model's second chance at the
    thing it actually got wrong."""
    room = await add_room(db, student)
    broken = json.dumps(
        {
            "questions": [question(correct="Paris", wrong=("paris", "a", "b"))]
            + [question(stem="Q2"), question(stem="Q3")]
        }
    )
    good = json.dumps({"questions": [question(stem=f"Q{i}") for i in range(3)]})
    use_llm(
        FakeLLM(
            answers=[broken, broken, good],
            # A rate limit before the first call, so the repairs that follow must
            # still both be available.
            errors=[ProviderUnavailable("429 rate limited"), None, None, None],
        )
    )

    status, body = await run_skill(client, auth, room, QUIZ, {"question_count": 3})

    assert status == 201
    assert body["status"] == "succeeded", "the repair budget was eaten by the retry"


async def test_a_skill_failure_keeps_the_providers_own_reason(
    client: AsyncClient,
    auth: dict[str, str],
    db: AsyncSession,
    student: Student,
) -> None:
    """A blocked response is not "the model could not be reached". The turn and
    the attempt row under it used to disagree in the same response, because every
    provider error collapsed to PROVIDER_UNAVAILABLE on the way out."""
    room = await add_room(db, student)
    use_llm(FakeLLM(error=ResponseBlocked("blocked on safety grounds")))

    status, body = await run_skill(client, auth, room, QUIZ, {"question_count": 3})

    assert status == 201
    assert body["failure_reason"] == "RESPONSE_BLOCKED"
    detail = (await client.get(f"/turns/{body['turn_id']}", headers=auth)).json()
    assert detail["failure_reason"] == "RESPONSE_BLOCKED"
    assert detail["attempts"][0]["error_type"] == "RESPONSE_BLOCKED"


async def test_the_repair_instruction_does_not_compound(
    client: AsyncClient,
    auth: dict[str, str],
    db: AsyncSession,
    student: Student,
) -> None:
    """Each repair is built from the original prompt, not from the last one. It
    used to append, so the third call carried the second call's repair block and
    the first call's problems — which by then described a draft that no longer
    existed."""
    room = await add_room(db, student)
    broken = json.dumps(
        {
            "questions": [question(correct="Paris", wrong=("paris", "a", "b"))]
            + [question(stem="Q2"), question(stem="Q3")]
        }
    )
    llm = use_llm(FakeLLM(answers=[broken, broken, broken]))

    await run_skill(client, auth, room, QUIZ, {"question_count": 3})

    assert len(llm.calls) == 3
    for call in llm.calls[1:]:
        assert call.prompt.count("rejected by automatic checks") == 1


# --- a skill run is a turn --------------------------------------------------


async def test_a_skill_run_is_a_turn_in_the_room(
    client: AsyncClient,
    auth: dict[str, str],
    db: AsyncSession,
    student: Student,
) -> None:
    """The shape the whole slice rests on. A quiz built in a room belongs in that
    room's conversation — the student asked for it there."""
    room = await add_room(db, student)
    use_llm(
        FakeLLM(
            answers=[
                json.dumps({"questions": [question(stem=f"Q{i}") for i in range(3)]})
            ]
        )
    )
    _, body = await run_skill(
        client, auth, room, QUIZ, {"topic": "photosynthesis", "question_count": 3}
    )

    timeline = (await client.get(f"/rooms/{room.id}/turns", headers=auth)).json()
    assert len(timeline["items"]) == 1
    entry = timeline["items"][0]
    assert entry["id"] == body["turn_id"]
    assert "quiz" in entry["student_message"].lower()
    assert "3-question quiz" in entry["answer_text"]
    # It counts as studying, so study history sees the topic.
    assert entry["concepts"] == ["photosynthesis"]


async def test_the_model_calls_are_inspectable_through_the_turn(
    client: AsyncClient,
    auth: dict[str, str],
    db: AsyncSession,
    student: Student,
) -> None:
    """One inspection endpoint for a quiz and a conversation alike. Before the
    rework these attempts lived in a JSONB column with a shape of their own."""
    room = await add_room(db, student)
    broken = [question(correct="Paris", wrong=("paris", "Rome", "Madrid"))] + [
        question(stem="Q2"),
        question(stem="Q3"),
    ]
    fixed = [question(stem=f"Q{i}") for i in range(3)]
    use_llm(
        FakeLLM(
            answers=[
                json.dumps({"questions": broken}),
                json.dumps({"questions": fixed}),
            ]
        )
    )
    _, body = await run_skill(client, auth, room, QUIZ, {"question_count": 3})

    detail = (await client.get(f"/turns/{body['turn_id']}", headers=auth)).json()

    assert [a["purpose"] for a in detail["attempts"]] == ["skill", "skill"]
    assert [a["prompt_name"] for a in detail["attempts"]] == [QUIZ, QUIZ]
    # The rejected attempt says which rule rejected it, in the same column the
    # tutor's content checks use.
    assert any("written twice" in f for f in detail["attempts"][0]["validation_failures"])
    assert detail["attempts"][1]["validation_failures"] is None
    assert detail["tokens"]["total"] > 0
    # And the finished quiz hangs off the same turn.
    assert len(detail["skill_run"]["questions"]) == 3


async def test_a_failed_skill_fails_its_turn_too(
    client: AsyncClient,
    auth: dict[str, str],
    db: AsyncSession,
    student: Student,
) -> None:
    """A turn that produced nothing showable must not read as succeeded, or the
    room timeline would claim work that never happened."""
    room = await add_room(db, student)
    broken = json.dumps(
        {
            "questions": [
                question(correct="Paris", wrong=("paris", "a", "b")),
                question(stem="Q2"),
                question(stem="Q3"),
            ]
        }
    )
    use_llm(FakeLLM(answers=[broken, broken, broken, broken]))

    _, body = await run_skill(client, auth, room, QUIZ, {"question_count": 3})

    detail = (await client.get(f"/turns/{body['turn_id']}", headers=auth)).json()
    assert detail["status"] == "failed"
    assert detail["failure_reason"] == "INVALID_AFTER_REPAIR"
    assert body["failure_reason"] == "CHECKS_FAILED"
    # Every rejected attempt is still there to read.
    assert len(detail["attempts"]) == 3


async def test_a_skill_gives_up_at_its_deadline(
    client: AsyncClient,
    auth: dict[str, str],
    db: AsyncSession,
    student: Student,
    monkeypatch,
) -> None:
    """Three slow-but-successful calls must not add up past what anyone waits for.

    Separate from the request timeout, which is the only thing that can end a
    call that hangs — this is the one that stops repairs accumulating.
    """
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "skill_deadline_seconds", 0)

    room = await add_room(db, student)
    use_llm(FakeLLM(answers=[json.dumps({"questions": [question()]})]))

    _, body = await run_skill(client, auth, room, QUIZ, {"question_count": 3})

    assert body["status"] == "failed"
    assert body["failure_reason"] == "DEADLINE_EXCEEDED"

    detail = (await client.get(f"/turns/{body['turn_id']}", headers=auth)).json()
    assert detail["failure_reason"] == "TURN_DEADLINE_EXCEEDED"


async def test_the_list_is_summaries_and_paged(
    client: AsyncClient,
    auth: dict[str, str],
    db: AsyncSession,
    student: Student,
) -> None:
    """A list is for choosing which run to open, so it must not carry the runs.

    Returning full runs made a single ten-question quiz a ~10 KB row, and read
    every question and option out of the database to have the serialiser throw
    them away.
    """
    room = await add_room(db, student)
    quiz_json = json.dumps({"questions": [question(stem=f"Q{i}") for i in range(3)]})
    use_llm(FakeLLM(answers=[quiz_json, quiz_json, quiz_json]))
    for _ in range(3):
        await run_skill(client, auth, room, QUIZ, {"question_count": 3})

    first = (
        await client.get(f"/rooms/{room.id}/skills/runs?limit=2", headers=auth)
    ).json()

    assert len(first["items"]) == 2
    assert first["next_cursor"] is not None
    row = first["items"][0]
    assert "questions" not in row and "checks" not in row and "result" not in row
    assert row["question_count"] == 3
    # One id, and it is the one every path takes.
    assert "id" not in row
    assert (
        await client.get(f"/turns/{row['turn_id']}/skill-run", headers=auth)
    ).status_code == 200

    second = (
        await client.get(
            f"/rooms/{room.id}/skills/runs?limit=2&cursor={first['next_cursor']}",
            headers=auth,
        )
    ).json()
    assert len(second["items"]) == 1
    assert second["next_cursor"] is None


async def test_a_turn_with_no_skill_run_says_so(
    client: AsyncClient, auth: dict[str, str], db: AsyncSession, student: Student
) -> None:
    """An ordinary tutor turn has no quiz hanging off it."""
    room = await add_room(db, student)
    created = await client.post(
        f"/rooms/{room.id}/turns", json={"message": "hello"}, headers=auth
    )
    turn_id = created.json()["id"]

    response = await client.get(f"/turns/{turn_id}/skill-run", headers=auth)
    assert response.status_code == 404
    assert "no skill run" in response.json()["detail"]


async def test_an_unknown_skill_has_no_route_at_all(
    client: AsyncClient, auth: dict[str, str], db: AsyncSession, student: Student
) -> None:
    """Stronger than the check it replaces. With a skill name in the path there
    was a handler that looked names up and refused unknown ones; with a route per
    skill there is nothing to refuse, because there is nothing to reach."""
    room = await add_room(db, student)
    status, _ = await run_skill(client, auth, room, "delete_everything", {})
    assert status == 404


async def test_each_skill_has_its_own_request_schema(client: AsyncClient) -> None:
    """The reason for a route per skill. One shared endpoint could only carry one
    example, so Swagger showed a quiz body on the solver — misleading, which is
    worse than showing nothing."""
    spec = (await client.get("/openapi.json")).json()

    quiz = spec["paths"]["/rooms/{room_id}/skills/quiz_builder/runs"]["post"]
    solver = spec["paths"]["/rooms/{room_id}/skills/step_solver/runs"]["post"]

    def fields(operation: dict) -> set[str]:
        ref = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        return set(spec["components"]["schemas"][ref.rsplit("/", 1)[-1]]["properties"])

    assert fields(quiz) == {"topic", "question_count"}
    assert fields(solver) == {"problem"}


async def test_another_students_run_is_invisible(
    client: AsyncClient,
    auth: dict[str, str],
    db: AsyncSession,
    student: Student,
    fake_llm: FakeLLM,
) -> None:
    room = await add_room(db, student)
    use_llm(
        FakeLLM(
            answers=[
                json.dumps(
                    {"questions": [question(stem=f"Q{i}") for i in range(3)]}
                )
            ]
        )
    )
    _, body = await run_skill(client, auth, room, QUIZ, {"question_count": 3})

    intruder = Student(display_name="Someone Else", education_level="Grade 9")
    db.add(intruder)
    await db.flush()

    response = await client.get(
        f"/turns/{body['turn_id']}/skill-run",
        headers={"X-Student-Id": str(intruder.id)},
    )
    assert response.status_code == 404
