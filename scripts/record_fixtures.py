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
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.llm import LLMRequest, get_llm_client  # noqa: E402
from app.prompts.loader import load_prompt  # noqa: E402

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
    prompt = load_prompt("tutor_system", 1)
    client = get_llm_client()

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
        print(
            f"[{index}/{len(SCENARIOS)}] {scenario['student_message'][:48]!r}\n"
            f"    finish={response.finish_reason} "
            f"prompt={response.prompt_tokens} "
            f"output={response.output_tokens} "
            f"thinking={response.thought_tokens}\n"
            f"    {response.text[:110]!r}"
        )


if __name__ == "__main__":
    asyncio.run(main())
