"""Skill: solve an equation, with every step checked before the student sees it.

The brief's own example of a skill is *"a step-by-step math explainer"*, and it
is a good example precisely because the naive version is a renamed prompt: ask
the model to "explain step by step" and it will, fluently, including the times it
drops a sign.

The difference here is that every step is checked by a computer algebra system
before it is stored, and a step that fails the check is sent back to be redone.
The student is never shown working that has not been verified.

## What is checked

1. **Every step against the one before it** — do they have the same solution set?
   That single rule catches any arithmetic slip without needing to know which
   operation the model claimed to perform. See `verify.py`.
2. **The final answer against the original problem** — substitute it back in. A
   chain can be individually valid and still not finish the job.

## What happens when a step cannot be checked

Word problems, calculus, anything with two unknowns: the checker says so rather
than guessing, and the step is stored with `verified: false`. It is **not**
treated as a failure — refusing to help with a question we cannot verify would be
worse than helping and being honest about which lines were confirmed. The
inspection endpoint shows exactly which steps were checked, so the distinction is
visible rather than implied.
"""

import logging
from typing import ClassVar

from pydantic import BaseModel, Field

from app.skills.base import Generation, Skill, SkillContext, SkillFailure, generate
from app.skills.verify import (
    MAX_STEP_LENGTH,
    UnparseableStep,
    answer_satisfies,
    parse_equation,
    steps_are_equivalent,
)

logger = logging.getLogger(__name__)

PROMPT_NAME = "step_solver"
PROMPT_VERSION = 1

MAX_STEPS = 12

# Unknowns allowed in the problem, and the reason this skill can say no.
#
# The character allow-list in `verify.py` decides whether sympy can *read* a
# string, which is not the same question as whether the string is maths. "Give me
# a quiz on TCP" is letters and spaces, so it passes, and implicit multiplication
# turns it into a product of fourteen single-letter symbols — a perfectly valid
# expression that is not a problem anybody asked to solve.
#
# Symbol count separates them cleanly, because real school algebra has one or two
# unknowns and prose has as many as it has distinct letters. And the bound is not
# arbitrary: above one unknown `_solutions` already returns None and the checker
# drops to its weaker fallback, so a problem with many symbols was never going to
# be verified. Refusing it says that out loud instead of spending three model
# calls to arrive at `CHECKS_FAILED`, which is the wrong reason.
MAX_UNKNOWNS = 3

REPAIR_INSTRUCTION = (
    "Your previous working was checked with a computer algebra system and "
    "rejected. Fix exactly these problems and reply with the whole solution "
    "again, in the same JSON shape:"
)


class SolveArgs(BaseModel):
    problem: str = Field(
        min_length=3,
        max_length=MAX_STEP_LENGTH,
        description="The equation to solve, for example `3x + 6 = 15`.",
        examples=["3x + 6 = 15", "2(x - 4) = 10"],
    )


class DraftStep(BaseModel):
    expression: str = Field(description="The equation after this step.")
    explanation: str = Field(description="What was done, in one sentence.")


class DraftSolution(BaseModel):
    steps: list[DraftStep]
    answer: str = Field(description="The final answer, such as `x = 3`.")


def check_solution(solution: DraftSolution, *, problem: str) -> list[str]:
    """Check the working with sympy. Returns problems written for the model.

    Unverifiable steps are not problems — see the module docstring. Only steps
    that are checkable *and wrong* come back here.
    """
    problems: list[str] = []

    if not solution.steps:
        return ["the solution has no steps"]
    if len(solution.steps) > MAX_STEPS:
        problems.append(f"there are {len(solution.steps)} steps, which is more than {MAX_STEPS}")

    previous = problem
    for index, step in enumerate(solution.steps, 1):
        outcome = steps_are_equivalent(previous, step.expression)
        if outcome.checkable and not outcome.ok:
            problems.append(f"step {index} is wrong: {outcome.detail}")
        if not step.explanation.strip():
            problems.append(f"step {index} has no explanation")
        previous = step.expression

    final = answer_satisfies(problem, solution.answer)
    if final.checkable and not final.ok:
        problems.append(f"the final answer is wrong: {final.detail}")

    return problems


def verified_steps(solution: DraftSolution, *, problem: str) -> list[dict]:
    """The steps, each recording whether a machine agreed with it.

    Stored on the run so the inspection endpoint can show that checking happened
    and which lines it covered. "We verify the maths" is a claim; this is the
    evidence.
    """
    steps = []
    previous = problem
    for index, step in enumerate(solution.steps, 1):
        outcome = steps_are_equivalent(previous, step.expression)
        steps.append(
            {
                "position": index,
                "expression": step.expression,
                "explanation": step.explanation,
                # Both flags, because "we could not check this" and "we checked it
                # and it is wrong" are different things to show a student, and a
                # single boolean would merge them into a shrug.
                "verified": outcome.ok and outcome.checkable,
                "checkable": outcome.checkable,
                "detail": outcome.detail or None,
            }
        )
        previous = step.expression
    return steps


class StepSolverSkill(Skill):
    name = "step_solver"
    version = PROMPT_VERSION
    args_model: ClassVar[type[BaseModel]] = SolveArgs

    def describe_request(self, args: SolveArgs) -> str:
        return f"Solve {args.problem} step by step."

    def summarise(self, args: SolveArgs, payload: DraftSolution) -> str:
        return (
            f"{payload.answer} — {len(payload.steps)} steps, each checked with a "
            f"computer algebra system."
        )

    async def run(self, args: SolveArgs, context: SkillContext) -> Generation:
        problem = args.problem.strip()

        # Refuse before spending a model call. If the checker cannot read the
        # problem it cannot verify any step of the answer, and this skill without
        # its checks is the renamed prompt the brief warns about — the tutor
        # endpoint already handles unstructured maths questions perfectly well.
        try:
            equation = parse_equation(problem)
        except UnparseableStep as exc:
            raise SkillFailure(
                "UNCHECKABLE_PROBLEM",
                f"This solver only handles equations it can verify — {exc}. Ask the "
                f"tutor directly for word problems or anything with several unknowns.",
            ) from exc

        # Parsing proves sympy could read it, not that it is a problem. See
        # MAX_UNKNOWNS — this is the test that keeps prose out.
        unknowns = getattr(equation, "free_symbols", set())
        if len(unknowns) > MAX_UNKNOWNS:
            raise SkillFailure(
                "UNCHECKABLE_PROBLEM",
                f"This solver only handles equations it can verify, and this one "
                f"has {len(unknowns)} unknowns. If it is a word problem or a "
                f"question in prose, ask the tutor directly.",
            )

        result = await generate(
            context,
            prompt_name=PROMPT_NAME,
            prompt_version=PROMPT_VERSION,
            variables={
                "education_level": context.student.education_level,
                "problem": problem,
            },
            schema=DraftSolution,
            check=lambda solution: check_solution(solution, problem=problem),
            repair_prompt=REPAIR_INSTRUCTION,
        )

        result.checks.append(
            {"verified_steps": verified_steps(result.payload, problem=problem)}
        )
        return result
