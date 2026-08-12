"""Record real Gemini responses as test fixtures.

Run once, with a valid key:

    docker compose exec -e LLM_FIXTURE_MODE=record api python scripts/record_fixtures.py

After that the whole suite can replay these offline: same answers every run, no
quota, no network. Recording real responses matters because hand-written fixtures
only ever test the model we *imagine* — these capture how it actually behaves,
including how much of its output budget it spends thinking.

Deliberately broken responses stay hand-written under
tests/fixtures/llm/synthetic/. The real model will not produce a quiz with
duplicate options on demand, which is exactly why those cannot be recorded.

**Re-record after changing a prompt.** A fixture is keyed by the exact request,
prompt text included, so an edited prompt makes every existing recording
unreachable rather than merely stale. Old files should be deleted when that
happens; they can never be served again.

Each response is also run through the real validation pipeline and the verdict
printed. That is the point of recording at all: it answers "does the model
actually comply with this prompt", with evidence, instead of assuming it does.
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.llm import LLMRequest, get_llm_client  # noqa: E402
from app.prompts.loader import load_prompt  # noqa: E402
from app.reliability.failures import ResponseInvalid  # noqa: E402
from app.reliability.pipeline import validate_tutor_response  # noqa: E402
from app.services.turn import TUTOR_PROMPT, TUTOR_PROMPT_VERSION  # noqa: E402

# Representative of what real students ask: a worked problem, a follow-up that
# depends on earlier context, a concept question, and one deliberately off-topic
# message to capture how the tutor declines.
SCENARIOS = [
    {
        "education_level": "Class 8",
        "room_title": "Grade 7 Algebra: Solving Equations",
        "student_message": "How do I solve 3x + 6 = 15?",
        "recent_turns": [],
    },
    {
        "education_level": "Class 8",
        "room_title": "Grade 7 Algebra: Solving Equations",
        "student_message": "Why did you subtract 6 first?",
        "recent_turns": [
            {
                "student_message": "How do I solve 3x + 6 = 15?",
                "answer_text": "Subtract 6 from both sides to get 3x = 9, then divide by 3.",
            }
        ],
    },
    {
        "education_level": "University, 2nd year",
        "room_title": "AP Biology: Cell Respiration",
        "student_message": "Explain how the electron transport chain generates ATP.",
        "recent_turns": [],
    },
    {
        "education_level": "Class 8",
        "room_title": "Grade 7 Algebra: Solving Equations",
        "student_message": "Who won the 2022 football world cup?",
        "recent_turns": [],
    },
]


class _Turn:
    """Duck-types the Turn attributes the template reads."""

    def __init__(self, student_message: str, answer_text: str) -> None:
        self.student_message = student_message
        self.answer_text = answer_text


async def main() -> None:
    if os.environ.get("LLM_FIXTURE_MODE") != "record":
        sys.exit("Set LLM_FIXTURE_MODE=record before running this.")

    settings = get_settings()
    # Whatever the service actually sends, so a recording can never be made
    # against a prompt version the application no longer uses.
    prompt = load_prompt(TUTOR_PROMPT, TUTOR_PROMPT_VERSION)
    client = get_llm_client()
    print(f"Recording against {prompt.name}@v{prompt.version}\n")

    accepted = 0
    for index, scenario in enumerate(SCENARIOS, start=1):
        rendered = prompt.render(
            education_level=scenario["education_level"],
            room_title=scenario["room_title"],
            recent_turns=[_Turn(**t) for t in scenario["recent_turns"]],
            student_message=scenario["student_message"],
        )
        request = LLMRequest(
            prompt=rendered,
            model=settings.gemini_model,
            temperature=prompt.temperature,
        )
        response = await client.generate(request)

        try:
            answer = validate_tutor_response(response.text)
        except ResponseInvalid as invalid:
            verdict = f"REJECTED at the {invalid.layer} layer"
            summary = ", ".join(f.code for f in invalid.failures)
        else:
            accepted += 1
            verdict = "accepted"
            summary = (
                f"in_scope={answer.in_scope} concepts={answer.concepts} "
                f"words={len(answer.answer.split())}"
            )

        print(
            f"[{index}/{len(SCENARIOS)}] {scenario['student_message'][:48]!r}\n"
            f"    finish={response.finish_reason} "
            f"prompt={response.prompt_tokens} "
            f"output={response.output_tokens} "
            f"thinking={response.thought_tokens}\n"
            f"    {verdict}: {summary}"
        )

    print(
        f"\n{accepted}/{len(SCENARIOS)} accepted first time. Anything rejected here "
        f"is a real prompt weakness worth fixing in a new version, not a fluke."
    )


if __name__ == "__main__":
    asyncio.run(main())
