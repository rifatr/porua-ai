"""Fill the database with a few weeks of plausible study history.

    docker compose exec api python scripts/seed_demo_data.py

Study history cannot be judged on two turns from this morning. This writes six
weeks of them so `GET /students/me/study-history` and the `query_study_history`
tool return something worth reading, and so the review call has something to look
at that is not a single "hello".

**No Gemini calls.** Rows are written straight to the database, which is what
makes this instant, free, and repeatable. That is legitimate for demo data and
would not be for a test: these turns never went near the reliability pipeline, so
they prove nothing about it. What they exercise is the *query* — grouping,
date filtering, and the rule that only successful in-scope turns count.

Deliberately includes turns that must **not** appear in history:

- a **failed** turn, which taught nothing;
- an **off-topic** turn, which is not revision of the room's subject.

If either ever shows up in the totals, the filter has broken and the numbers are
quietly wrong — the kind of bug that is invisible until someone checks by hand.
"""

import asyncio
import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: E402

from app.db.session import engine  # noqa: E402
from app.models.room import Room  # noqa: E402
from app.models.student import Student  # noqa: E402
from app.models.turn import Turn, TurnFailureReason, TurnStatus  # noqa: E402

DISPLAY_NAME = "Demo Student"
EDUCATION_LEVEL = "Class 9"

# Fixed so two runs produce the same history. A demo that looks different every
# time is one you cannot write instructions for.
SEED = 20260815

ROOMS: dict[str, list[tuple[str, str, list[str]]]] = {
    "Grade 9 Algebra: Solving Equations": [
        ("How do I solve 3x + 6 = 15?", "Subtract 6, then divide by 3. x = 3.",
         ["inverse operations", "linear equations"]),
        ("Why do I subtract first?", "You undo in the opposite order to how it was built.",
         ["inverse operations", "order of operations"]),
        ("What about 2(x + 4) = 18?", "Expand or divide by 2 first — both work. x = 5.",
         ["distributive law", "linear equations"]),
        ("How do I check my answer?", "Substitute it back into the original equation.",
         ["checking a solution", "substitution"]),
        ("What if x is on both sides?", "Collect the x terms on one side first.",
         ["collecting like terms", "linear equations"]),
    ],
    "AP Biology: Cell Respiration": [
        ("What is ATP for?", "It is the cell's usable energy currency.",
         ["atp", "energy transfer"]),
        ("Explain glycolysis simply.", "Glucose is split into two pyruvate, netting 2 ATP.",
         ["glycolysis", "atp"]),
        ("Where does the Krebs cycle happen?", "In the mitochondrial matrix.",
         ["krebs cycle", "mitochondria"]),
        ("What is the electron transport chain?", "A chain of carriers that pumps protons.",
         ["electron transport chain", "mitochondria"]),
    ],
    "Physics: Light and Reflection": [
        ("Why can I see a ball?", "Light bounces off it and reaches your eye.",
         ["light reflection", "eye parts"]),
        ("What is the law of reflection?", "The angle in equals the angle out.",
         ["light reflection", "angles"]),
        ("Why is a mirror image flipped?", "Left and right swap because the light reverses.",
         ["light reflection", "mirrors"]),
    ],
    "Grade 9 Geometry: Triangles": [
        ("What is Pythagoras' theorem?", "a squared plus b squared equals c squared.",
         ["pythagoras", "right triangles"]),
        ("When can I use it?", "Only on right-angled triangles.",
         ["pythagoras", "right triangles"]),
    ],
}


async def main() -> None:
    random.seed(SEED)
    now = datetime.now(UTC)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as db:
        existing = await db.scalar(
            select(Student).where(Student.display_name == DISPLAY_NAME)
        )
        if existing is not None:
            print(f"'{DISPLAY_NAME}' already exists: {existing.id}")
            print("Delete that student first if you want a clean seed.")
            return

        student = Student(display_name=DISPLAY_NAME, education_level=EDUCATION_LEVEL)
        db.add(student)
        await db.flush()

        total = 0
        # Oldest room first, so the most recently studied one is last and lands at
        # the top of the room list.
        for room_index, (title, exchanges) in enumerate(reversed(list(ROOMS.items()))):
            days_ago_base = 38 - room_index * 9
            room = Room(student_id=student.id, title=title)
            db.add(room)
            await db.flush()

            for seq, (question, answer, concepts) in enumerate(exchanges, start=1):
                # Spread within the room, always in the past, never out of order.
                days_ago = max(0, days_ago_base - seq * 2 - random.randint(0, 1))
                when = now - timedelta(days=days_ago, hours=random.randint(0, 9))

                db.add(
                    Turn(
                        room_id=room.id,
                        seq=seq,
                        student_message=question,
                        answer_text=answer,
                        status=TurnStatus.SUCCEEDED.value,
                        in_scope=True,
                        concepts=concepts,
                        created_at=when,
                        completed_at=when + timedelta(seconds=4),
                    )
                )
                total += 1

            room.last_activity_at = now - timedelta(days=max(0, days_ago_base - 10))

        # The two that must never appear in history. Both go in the first room.
        algebra = await db.scalar(
            select(Room).where(Room.student_id == student.id).order_by(Room.title)
        )
        noise_at = now - timedelta(days=3)
        db.add(
            Turn(
                room_id=algebra.id,
                seq=90,
                student_message="Who won the 2022 football world cup?",
                answer_text="This room is for algebra — try a new room for that.",
                status=TurnStatus.SUCCEEDED.value,
                in_scope=False,
                concepts=[],
                created_at=noise_at,
                completed_at=noise_at + timedelta(seconds=2),
            )
        )
        db.add(
            Turn(
                room_id=algebra.id,
                seq=91,
                student_message="Explain the quadratic formula",
                status=TurnStatus.FAILED.value,
                failure_reason=TurnFailureReason.PROVIDER_UNAVAILABLE.value,
                created_at=noise_at,
                completed_at=noise_at + timedelta(seconds=1),
            )
        )

        await db.commit()

        print(f"Seeded {total} study turns across {len(ROOMS)} rooms.")
        print("Plus 1 off-topic and 1 failed turn, which history must exclude.\n")
        print(f"  export SID={student.id}\n")
        print("  curl -s localhost:8000/students/me/study-history \\")
        print('    -H "X-Student-Id: $SID" | jq\n')
        print("  # or ask the tutor, which reaches the same data through a tool:")
        print(f"  curl -s -X POST localhost:8000/rooms/{algebra.id}/turns \\")
        print("    -H 'Content-Type: application/json' -H \"X-Student-Id: $SID\" \\")
        print("    -d '{\"message\":\"what have I studied over the last month?\"}' | jq")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
