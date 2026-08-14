"""Checking algebra, so the student is never shown a wrong step.

This module is the answer to *"how is your skill not just a renamed prompt?"*.
A prompt that says "explain step by step" produces steps that **look** right. This
produces steps that **are** right, because each one is checked against the one
before it by a computer algebra system, and a step that fails the check never
reaches the student.

## What "correct" means for a step

A solving step rewrites an equation without changing its solutions:

    3x + 6 = 15      ->      3x = 9      ->      x = 3

Each of those has exactly one solution, `x = 3`. So the check is not "does this
line look like the last one with 6 subtracted" — it is **do these two equations
have the same solution set**. That catches every arithmetic slip, in one rule,
without knowing which operation the model claimed to perform.

## Why sympy is not handed the string directly

`sympify` runs Python. It has had sandbox escapes, and the string here originates
with a student and passes through a language model, so it is untrusted twice over.
The same posture as `evaluate_expression` in S4 applies: an allow-list first, then
the library.

`_ACCEPTABLE` admits digits, single letters, the four operators, `^`, brackets,
`=` and `.` — and nothing else. No quotes, no underscores, no dots followed by a
name, so no attribute access and no dunder. A string that fails it never reaches
sympy at all.

## The allow-list stops code, not cost

That is a separate problem and it needs a separate answer. `9^9^9` is six
characters, every one of them allowed, and evaluating it asks Python for a
370-million-digit integer — on the event loop, with nothing able to interrupt it.
`x^99999` parses in a millisecond and then spends forever in `solveset`. Neither
is an escape; both are a hang, which for a synchronous API is the same outcome.

So the guards below bound the *work*, not just the characters, and the two bounds
are different because the costs are different — see `_reject_runaway_powers`.
"""

import logging
import re
from dataclasses import dataclass

from sympy import Eq, Pow, solveset
from sympy.core.sympify import SympifyError
from sympy.parsing.sympy_parser import (
    implicit_multiplication_application,
    parse_expr,
    standard_transformations,
)

logger = logging.getLogger(__name__)

# Long enough for school algebra, short enough that nothing pathological parses.
MAX_STEP_LENGTH = 200

# Two ceilings on `^`, because a power costs two different things depending on
# what it is raised from, and one number cannot express both.
#
# A number to a number costs the size of the result: `2^1000` is a 302-digit
# integer and instant. Same limit as `MAX_EXPONENT` in app/tools/calculator.py,
# which bounds the same thing for the same reason.
MAX_NUMERIC_EXPONENT = 1000
# A *variable* to a number costs `solveset`, which is polynomial degree, and that
# is far more expensive than it looks. Measured: degree 20 takes 0.4s, degree 50
# takes 2.2s, degree 100 takes 14.6s. School algebra does not go past degree four,
# so 20 is already generous and 100 is a request that never returns in time.
MAX_POLYNOMIAL_DEGREE = 20

# The allow-list. Deliberately narrow: letters are variables, and there is no way
# to write a dotted name, a string, or an underscore.
_ACCEPTABLE = re.compile(r"^[0-9a-zA-Z+\-*/^().=\s]+$")

# `implicit_multiplication_application` is what makes `3x` parse as `3*x`, which
# is how a student writes it and therefore how the model will echo it back.
_TRANSFORMS = standard_transformations + (implicit_multiplication_application,)


class UnparseableStep(Exception):
    """The step could not be read as maths. Not the same as being wrong."""


@dataclass(frozen=True)
class StepCheck:
    ok: bool
    detail: str
    # False when the checker reached no verdict at all — the step is outside what
    # it can decide, rather than wrong.
    #
    # Callers must gate on this before reading `ok`, and both directions matter:
    # reporting these as failures would reject working that is probably fine,
    # and counting them as verified would claim a machine agreed with a line it
    # never looked at. This used to be inferred by substring-matching `detail`
    # in two callers, which meant editing a message here silently changed what
    # the solver rejected.
    checkable: bool = True


def _reject_runaway_powers(tree) -> None:
    """Refuse powers that are cheap to write and impossible to finish.

    Runs on an *unevaluated* tree, which is the whole trick: `parse_expr` with
    `evaluate=False` builds `9**9**9` as three nodes in microseconds, where
    evaluating it first would already have hung. So the guard gets to look before
    any arithmetic happens.

    An exponent that is itself a calculation is refused rather than measured —
    the same rule, for the same reason, as `_check_power` in
    app/tools/calculator.py: you cannot check the size of `9^9` without computing
    it, and computing it is the thing being guarded against.
    """
    for power in tree.atoms(Pow):
        base, exponent = power.base, power.exp
        if not exponent.is_Number:
            if base.is_Number:
                raise UnparseableStep(
                    "the exponent must be a plain number, not another calculation"
                )
            continue  # `x**n` with a symbolic exponent stays symbolic and costs nothing
        limit = MAX_NUMERIC_EXPONENT if base.is_Number else MAX_POLYNOMIAL_DEGREE
        if abs(exponent) > limit:
            raise UnparseableStep(
                f"the power {exponent} is larger than this checker will attempt "
                f"(limit {limit})"
            )


def _parse_side(text: str):
    """One side of an equation, as a sympy expression.

    Parsed twice on purpose. The first pass builds the tree without doing the
    arithmetic, so `_reject_runaway_powers` can veto it; only what survives is
    parsed again for real.
    """
    cleaned = text.strip().replace("^", "**")
    try:
        tree = parse_expr(cleaned, transformations=_TRANSFORMS, evaluate=False)
    except (SympifyError, SyntaxError, TypeError, AttributeError, ValueError) as exc:
        raise UnparseableStep(f"could not read {text!r} as maths") from exc

    _reject_runaway_powers(tree)

    try:
        return parse_expr(cleaned, transformations=_TRANSFORMS, evaluate=True)
    except (SympifyError, SyntaxError, TypeError, AttributeError, ValueError) as exc:
        raise UnparseableStep(f"could not read {text!r} as maths") from exc


def parse_equation(text: str):
    """`"3x + 6 = 15"` -> `Eq(3*x + 6, 15)`.

    An expression with no `=` becomes `Eq(expr, 0)`, so "simplify this" problems
    go through the same path as equations.
    """
    if not text or len(text) > MAX_STEP_LENGTH:
        raise UnparseableStep("the step is empty or too long to check")
    if not _ACCEPTABLE.match(text):
        raise UnparseableStep("the step contains characters this checker does not accept")
    if text.count("=") > 1:
        raise UnparseableStep("the step has more than one '='")

    if "=" in text:
        left, right = text.split("=")
        return Eq(_parse_side(left), _parse_side(right))
    return Eq(_parse_side(text), 0)


def _solutions(equation):
    """The solution set, or None when there is nothing to solve for.

    `solveset` rather than `solve` because it returns a set, which compares
    cleanly and says "no solutions" and "every value" without special cases.
    """
    symbols = sorted(equation.free_symbols, key=str)
    if not symbols:
        # No variable: `6 = 6` or `6 = 7`. Truth is the whole check.
        return bool(equation)
    if len(symbols) > 1:
        return None
    return solveset(equation, symbols[0])


def steps_are_equivalent(before: str, after: str) -> StepCheck:
    """Does `after` have the same solutions as `before`?

    This is the check. It knows nothing about what operation was claimed, which
    is what makes it hard to fool: a step is accepted because it preserves the
    answer, not because its explanation sounded reasonable.
    """
    try:
        first, second = parse_equation(before), parse_equation(after)
    except UnparseableStep as exc:
        return StepCheck(False, str(exc), checkable=False)

    left, right = _solutions(first), _solutions(second)

    if left is None or right is None:
        if not (isinstance(first, Eq) and isinstance(second, Eq)):
            # One side collapsed to a boolean — sympy evaluates `Eq(6, 6)` to
            # `True`, which has no `lhs` to subtract — and the other has several
            # unknowns, so neither route below applies. Reaching for `.lhs` here
            # was an AttributeError, and an AttributeError in a checker is a 500
            # on a path whose entire job is to fail in the open.
            return StepCheck(
                False,
                "the step is outside what this checker can decide",
                checkable=False,
            )
        # More than one variable. Fall back to asking whether the difference
        # between the two equations cancels out — weaker than solving, but it
        # still catches an arithmetic slip.
        same = ((first.lhs - first.rhs) - (second.lhs - second.rhs)).simplify() == 0
        return StepCheck(
            same,
            "" if same else f"{after!r} is not equivalent to {before!r}",
        )

    if left == right:
        return StepCheck(True, "")
    return StepCheck(
        False,
        f"{after!r} does not have the same solution as {before!r} "
        f"({right} instead of {left})",
    )


def answer_satisfies(problem: str, answer: str) -> StepCheck:
    """Put the answer back into the original problem and see if it holds.

    A separate check from the chain of steps, and worth doing even when every
    step passed: the steps prove the model transformed the equation faithfully,
    and this proves the thing it ended on is actually a solution to what was
    asked. A chain can be individually valid and still not finish the job.
    """
    try:
        original = parse_equation(problem)
        stated = parse_equation(answer)
    except UnparseableStep as exc:
        return StepCheck(False, str(exc), checkable=False)

    symbols = sorted(original.free_symbols, key=str)
    if not symbols:
        return StepCheck(bool(original), "")
    if (
        len(symbols) > 1
        # `Eq(2, 2)` and `Eq(x, x)` both collapse to a plain boolean with no
        # sides to read, so this has to come before `.lhs` rather than after it.
        or not isinstance(stated, Eq)
        or not isinstance(stated.lhs, type(symbols[0]))
    ):
        # "x = 3" is what we can check. Anything else is reported as no verdict
        # rather than as a failure: refusing to answer a question we cannot
        # verify would be worse than answering it with the steps checked and this
        # one skipped.
        return StepCheck(False, "final answer not in a checkable form", checkable=False)

    try:
        substituted = original.subs(stated.lhs, stated.rhs)
    except (TypeError, ValueError) as exc:  # pragma: no cover - sympy edge cases
        return StepCheck(False, f"could not substitute the answer: {exc}")

    holds = bool(substituted)
    return StepCheck(
        holds,
        "" if holds else f"substituting {answer!r} does not satisfy {problem!r}",
    )
