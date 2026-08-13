"""The calculator, and everything it refuses to do.

This is the most security-sensitive file in the project, so it gets the most
hostile tests. The attacks below are the real ones — the subclass walk in
particular is the standard route from a sandboxed `eval` to the file system, and
it is worth seeing it fail rather than assuming it would.

The claim being tested is not "we thought of these inputs". It is that only seven
node types can execute, so anything needing an attribute, a name, an import or a
loop cannot be expressed at all.
"""

import pytest

from app.tools.base import ToolFailure
from app.tools.calculator import evaluate_expression


def rejects(expression: str) -> str:
    with pytest.raises(ToolFailure) as caught:
        evaluate_expression(expression)
    return caught.value.detail


# --- the arithmetic actually works ------------------------------------------


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("2 + 3", 5),
        ("(3 * 4 + 2) / 7", 2.0),
        ("10 - 2 * 3", 4),
        ("7 // 2", 3),
        ("7 % 2", 1),
        ("2 ** 10", 1024),
        ("-5 + 3", -2),
        ("sqrt(144)", 12.0),
        ("abs(-7)", 7),
        ("round(3.14159, 2)", 3.14),
        ("max(3, 9, 2)", 9),
        ("factorial(5)", 120),
        ("gcd(12, 18)", 6),
    ],
)
def test_it_computes(expression: str, expected: float) -> None:
    assert evaluate_expression(expression) == expected


def test_constants_are_available() -> None:
    assert round(evaluate_expression("pi"), 5) == 3.14159


# --- code execution ---------------------------------------------------------


@pytest.mark.parametrize(
    "attack",
    [
        # Straight to the shell.
        "__import__('os').system('ls')",
        # The standard escape from a sandboxed eval: from any object reach
        # __class__, then __bases__, then __subclasses__(), and out to a file
        # handle from there.
        "(1).__class__.__bases__[0].__subclasses__()",
        "().__class__.__mro__[1].__subclasses__()",
        # Reading files.
        "open('/etc/passwd').read()",
        # Building a callable in place.
        "(lambda: 1)()",
        # Reaching for builtins by name.
        "eval('1+1')",
        "exec('x=1')",
        "globals()",
        "getattr(1, 'real')",
        # Comprehensions and generators are code, not arithmetic.
        "[i for i in range(10)]",
        "sum(i for i in range(10))",
    ],
)
def test_code_execution_is_impossible(attack: str) -> None:
    """Every one of these is refused before anything runs.

    The assertion is deliberately only that it was *rejected*, not which guard
    caught it. Several of these trip more than one rule, and which one fires first
    depends on `ast.walk` order — pinning that would make the test fragile without
    making the guarantee any stronger. The guarantee is that nothing gets through.
    """
    rejects(attack)


def test_attribute_access_is_impossible() -> None:
    """The single most important line in the file.

    Every escape above starts with a `.`. `ast.Attribute` is not an allowed node,
    so it cannot be written at all — which is why the list of attacks above does
    not need to be complete.
    """
    assert "Attribute is not allowed" in rejects("os.path")


def test_calling_an_undeclared_function_is_refused() -> None:
    detail = rejects("open('/etc/passwd')")
    assert "Unknown function 'open'" in detail
    assert "sqrt" in detail, "the message lists what is allowed, so the model can retry"


def test_variables_do_not_exist() -> None:
    assert "no variables here" in rejects("x + 1")


def test_assignment_is_not_an_expression() -> None:
    assert "not a valid expression" in rejects("x = 1")


def test_strings_are_refused() -> None:
    """`"a" * 10**9` is a memory bomb made only of otherwise-allowed nodes."""
    assert "Only numbers" in rejects("'a' * 1000000000")


# --- denial of service ------------------------------------------------------


def test_a_huge_power_is_refused_before_it_runs() -> None:
    """`9**9**9` has about 370 million digits. Every node in it is allowed, so
    the node allow-list cannot catch it — only a size check can."""
    assert "too large" in rejects("9 ** 99999")


def test_a_computed_exponent_is_refused() -> None:
    """The size of `9 ** (3 * 5000)` cannot be known without evaluating it, and
    evaluating it is the thing being avoided."""
    assert "must be a plain number" in rejects("9 ** (3 * 5000)")


def test_the_tower_of_powers_is_refused() -> None:
    """The bug this test exists for.

    The checks on the syntax tree can only inspect a base that is *written down*.
    Make the base a calculation and they see nothing: every node here is allowed,
    both exponents are plain numbers well under the limit, and the result has
    fifteen million digits. It ran for over eight seconds and was still going.

    Worse than a slow turn: this is synchronous CPU work, so the per-tool timeout
    cannot interrupt it. The event loop stops and every other request to the API
    stops with it. One chat message would take the process down.
    """
    assert "digits" in rejects("((10**15) ** 1000) ** 1000")


def test_deeper_towers_are_refused_too() -> None:
    assert "digits" in rejects("(((((10**15)**1000)**1000)**1000)**1000)**1000")


def test_a_huge_factorial_raised_to_a_power_is_refused() -> None:
    """`factorial(1000)` is allowed on its own and is 2,568 digits. Raised to the
    thousandth power it is two and a half million, and the base is a call rather
    than a literal, so only the size check at evaluation time sees it."""
    assert "digits" in rejects("factorial(1000) ** 1000")


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("(3 + 4) ** 2", 49),  # a computed base is fine when the result is small
        ("2 ** -3", 0.125),  # a negative exponent is ordinary maths
        ("10 ** -2", 0.01),
        ("(-2) ** 3", -8),
        ("2 ** 0", 1),
    ],
)
def test_the_size_guards_do_not_refuse_ordinary_powers(
    expression: str, expected: float
) -> None:
    """The guards must not cost a student their homework.

    `2 ** -3` is the one to watch: `-3` is not a constant in the syntax tree, it
    is unary minus applied to `3`. A naive "is this a literal number" check
    rejects it, and rejecting 0.125 to prevent a fork bomb is a bad trade.
    """
    assert evaluate_expression(expression) == expected


def test_a_huge_factorial_is_refused() -> None:
    assert "too large" in rejects("factorial(999999)")


def test_a_very_long_expression_is_refused() -> None:
    assert "too long" in rejects("1+" * 400 + "1")


# --- ordinary arithmetic errors ---------------------------------------------


def test_division_by_zero_is_a_clean_refusal() -> None:
    assert "Division by zero" in rejects("1 / 0")


def test_maths_domain_errors_are_reported_not_raised() -> None:
    """sqrt(-1) is the model's mistake, not a crash. It gets told why."""
    assert "Could not compute" in rejects("sqrt(-1)")


def test_a_non_finite_result_is_refused() -> None:
    assert "not a finite number" in rejects("1e308 * 10")


def test_nonsense_gets_a_syntax_message() -> None:
    assert "not a valid expression" in rejects("3 +* 4")
