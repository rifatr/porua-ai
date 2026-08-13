"""A calculator the model can use, that cannot run anything else.

Read this file carefully. It takes a string written by a language model and
evaluates it, which is the most dangerous shape a function can have, and it is the
file most likely to be picked apart on review.

## Why not `eval`

`eval("__import__('os').system('rm -rf /')")` is a working expression. So is
`eval("(1).__class__.__bases__[0].__subclasses__()")`, which walks from an integer
to every class Python has loaded and out to the file system from there. There is no
list of strings that reliably blocks this, because the attacker chooses the string
and Python's object graph is fully connected. Filtering input to `eval` is a losing
game; the fix is to never call it.

## What happens instead

The expression is parsed into a syntax tree, and every node in that tree is checked
against an allow-list. Anything not named below is rejected *before* any of it
runs. So the argument is not "we thought of the bad inputs" — it is "only these
seven node types can execute, and none of them can reach an attribute, a name, an
import, or a loop".

Specifically not on the list, and therefore impossible:

- `ast.Attribute` — no `.__class__`, so the subclass walk above cannot start
- `ast.Name`, except three read-only constants — no variables, no builtins
- `ast.Import`, `ast.Call` to anything unlisted — no `os`, no `open`
- comprehensions, lambdas, assignments, loops — not expressions we accept

## The one thing an allow-list does not stop

`9**9**9` is made only of allowed nodes and would hang the process computing a
number with hundreds of millions of digits. Size limits, not node types, are what
stop that — see `_check_power`.
"""

import ast
import math
from typing import ClassVar

from pydantic import BaseModel, Field

from app.tools.base import Tool, ToolContext, ToolFailure

# Only these node types may appear anywhere in the tree.
_ALLOWED_NODES = (
    ast.Expression,
    ast.Constant,
    ast.BinOp,
    ast.UnaryOp,
    ast.Call,
    ast.Name,
    ast.Load,
)

_ALLOWED_OPERATORS = (
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.USub,
    ast.UAdd,
)

# Every function the model may call, by name. `math` is not imported into scope —
# the names are bound one at a time here, so `math.system` is not reachable even
# if `math` had such an attribute.
_ALLOWED_FUNCTIONS = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sqrt": math.sqrt,
    "log": math.log,
    "log10": math.log10,
    "exp": math.exp,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "degrees": math.degrees,
    "radians": math.radians,
    "factorial": math.factorial,
    "gcd": math.gcd,
    "floor": math.floor,
    "ceil": math.ceil,
}

_ALLOWED_CONSTANTS = {"pi": math.pi, "e": math.e, "tau": math.tau}

# Guards against expressions that are legal but would not finish. A student's
# arithmetic never comes near these.
MAX_EXPRESSION_LENGTH = 500
MAX_EXPONENT = 1000
MAX_BASE_FOR_POWER = 10**15
MAX_FACTORIAL_INPUT = 1000

# The ceiling on the *result* of a power, checked while evaluating rather than
# while validating. About 30,000 decimal digits — beyond any arithmetic a student
# will ever ask for, and instant to compute.
#
# This exists because the tree checks below are not enough on their own. They can
# only inspect a base that is written as a literal, so a base that is itself a
# calculation slips past them:
#
#     ((10**15) ** 1000) ** 1000
#
# Every node there is allowed, both exponents are plain numbers under the limit,
# and the result has fifteen million digits. It ran for over eight seconds in
# testing and was still going.
#
# That is worse than a slow turn. It is synchronous CPU work, so the per-tool
# `asyncio.wait_for` cannot interrupt it — the whole event loop stops, and every
# other request to the API stops with it. One chat message would take the process
# down. Bounding the work is the only defence that actually applies.
MAX_RESULT_BITS = 100_000


class CalculatorArgs(BaseModel):
    expression: str = Field(
        description=(
            "A single arithmetic expression, e.g. '(3 * 4 + 2) / 7' or "
            "'sqrt(144)'. Numbers and operators only — no variables, no equations, "
            "no '=' sign."
        ),
        examples=["(3 * 4 + 2) / 7", "sqrt(144)", "factorial(5)"],
    )


def _literal_number(node: ast.AST) -> float | int | None:
    """The value of a written-down number, or None if it is a calculation.

    `-3` is not an `ast.Constant`. It parses as unary minus applied to `3`, so a
    plain `isinstance(node, ast.Constant)` check rejects `2 ** -3` — ordinary
    maths a student will ask for. Handling the sign here keeps the size guards
    strict without them refusing negative exponents.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub | ast.UAdd):
        inner = _literal_number(node.operand)
        if inner is not None:
            return -inner if isinstance(node.op, ast.USub) else inner
    return None


def _check_power(node: ast.BinOp) -> None:
    """Reject powers that are legal Python but would never finish.

    `9**9**9` is an integer with roughly 370 million digits. Python will try, take
    every core it can and all available memory with it. The allow-list cannot see
    this — every node in it is permitted — so it is checked by size instead.
    """
    exponent = _literal_number(node.right)
    if exponent is None:
        # A computed exponent cannot be checked before running it, and running it
        # is the thing we are trying to avoid.
        raise ToolFailure(
            "The exponent must be a plain number, not another calculation."
        )
    if abs(exponent) > MAX_EXPONENT:
        raise ToolFailure(
            f"Exponent {exponent} is too large; the limit is {MAX_EXPONENT}."
        )

    # Only checks a base that is written down. A base that is itself a calculation
    # is caught later by `_check_result_size`, during evaluation, where its value
    # is actually known — that is the case this check cannot see.
    base = _literal_number(node.left)
    if base is not None and abs(base) > MAX_BASE_FOR_POWER:
        raise ToolFailure("The base of the power is too large.")


def _check_call(node: ast.Call) -> str:
    """Allow a call only to a bare name on the list."""
    # Not `ast.Name` means something like `foo.bar()` or `(lambda: 1)()`. Both are
    # already impossible because Attribute and Lambda are not allowed nodes; this
    # is the second lock on the same door.
    if not isinstance(node.func, ast.Name):
        raise ToolFailure("Only direct function calls are allowed.")
    if node.func.id not in _ALLOWED_FUNCTIONS:
        allowed = ", ".join(sorted(_ALLOWED_FUNCTIONS))
        raise ToolFailure(f"Unknown function {node.func.id!r}. Allowed: {allowed}.")
    if node.keywords:
        raise ToolFailure("Functions take positional arguments only.")
    if node.func.id == "factorial":
        argument = node.args[0] if node.args else None
        if isinstance(argument, ast.Constant) and isinstance(argument.value, int):
            if argument.value > MAX_FACTORIAL_INPUT:
                raise ToolFailure(
                    f"factorial({argument.value}) is too large; the limit is "
                    f"{MAX_FACTORIAL_INPUT}."
                )
        else:
            raise ToolFailure("factorial takes a plain whole number.")
    return node.func.id


def _validate(tree: ast.Expression) -> None:
    """Walk every node and reject anything not explicitly allowed."""
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES + _ALLOWED_OPERATORS):
            raise ToolFailure(
                f"{type(node).__name__} is not allowed here. This calculator takes "
                f"numbers, + - * / // % **, and a fixed list of functions."
            )

        if isinstance(node, ast.Constant) and not isinstance(node.value, int | float):
            # Strings and bytes are constants too. `"a" * 10**9` is a memory bomb
            # built entirely from allowed nodes.
            raise ToolFailure("Only numbers are allowed, not text.")

        # A Name is either a constant like `pi`, or the callee of an allowed call.
        # Anything else would be a variable, and there are none.
        if (
            isinstance(node, ast.Name)
            and node.id not in _ALLOWED_CONSTANTS
            and node.id not in _ALLOWED_FUNCTIONS
        ):
            raise ToolFailure(
                f"Unknown name {node.id!r}. There are no variables here — "
                f"substitute the number yourself."
            )

        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            _check_power(node)

        if isinstance(node, ast.Call):
            _check_call(node)


def _check_result_size(base: float | int, exponent: float | int) -> None:
    """Refuse a power whose result would be enormous, before computing it.

    Runs during evaluation, not validation, and that is the whole point: here the
    base is a *number*, however it was written. `_check_power` can only inspect a
    literal, so it is blind to `((10**15) ** 1000) ** 1000` — by the time this runs
    the inner value is known and the size is arithmetic on the exponent.

    Estimating rather than computing: the bit length of `b ** e` is about
    `e * bit_length(b)`. Multiplying two integers is instant; computing the power
    to find out how big it is would be doing the exact thing being prevented.
    """
    if exponent <= 0 or base in (0, 1, -1):
        return

    if isinstance(base, float) or isinstance(exponent, float):
        # Floats cannot get large enough to hang anything — they overflow to inf
        # instead, which `evaluate_expression` already rejects as non-finite.
        return

    bits = exponent * max(1, abs(base).bit_length())
    if bits > MAX_RESULT_BITS:
        raise ToolFailure(
            f"That result would have roughly {int(bits / 3.32):,} digits, which is "
            f"far past anything useful. Simplify the calculation."
        )


def _evaluate(node: ast.AST) -> float | int:
    """Compute the value of an already-validated tree.

    Recursion over the node types rather than `eval` on the tree, so nothing
    outside these branches can execute even if validation were bypassed.
    """
    match node:
        case ast.Expression():
            return _evaluate(node.body)

        case ast.Constant():
            return node.value

        case ast.Name():
            return _ALLOWED_CONSTANTS[node.id]

        case ast.UnaryOp():
            value = _evaluate(node.operand)
            return -value if isinstance(node.op, ast.USub) else +value

        case ast.BinOp():
            left, right = _evaluate(node.left), _evaluate(node.right)
            match node.op:
                case ast.Add():
                    return left + right
                case ast.Sub():
                    return left - right
                case ast.Mult():
                    return left * right
                case ast.Div():
                    if right == 0:
                        raise ToolFailure("Division by zero.")
                    return left / right
                case ast.FloorDiv():
                    if right == 0:
                        raise ToolFailure("Division by zero.")
                    return left // right
                case ast.Mod():
                    if right == 0:
                        raise ToolFailure("Division by zero.")
                    return left % right
                case ast.Pow():
                    _check_result_size(left, right)
                    return left**right

        case ast.Call():
            function = _ALLOWED_FUNCTIONS[node.func.id]  # type: ignore[attr-defined]
            return function(*[_evaluate(argument) for argument in node.args])

    raise ToolFailure(f"Cannot evaluate {type(node).__name__}.")


def evaluate_expression(expression: str) -> float | int:
    """Parse, validate, then compute. Raises ToolFailure on anything unacceptable."""
    if len(expression) > MAX_EXPRESSION_LENGTH:
        raise ToolFailure(
            f"Expression is too long ({len(expression)} characters, "
            f"limit {MAX_EXPRESSION_LENGTH})."
        )

    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError as exc:
        raise ToolFailure(f"That is not a valid expression: {exc.msg}.") from exc

    _validate(tree)

    try:
        result = _evaluate(tree)
    except ToolFailure:
        raise
    except (ValueError, OverflowError, ZeroDivisionError) as exc:
        # sqrt(-1), log(0), a float that overflows. The model gets the reason and
        # can decide what to do; it is not our bug.
        raise ToolFailure(f"Could not compute that: {exc}.") from exc

    if isinstance(result, float) and not math.isfinite(result):
        raise ToolFailure("The result is not a finite number.")

    return result


class CalculatorTool(Tool):
    name = "evaluate_expression"
    description = (
        "Compute the value of an arithmetic expression exactly. Use this whenever "
        "a number matters — language models make arithmetic mistakes, and a wrong "
        "number in a worked example teaches the student something false. "
        "Takes numbers and + - * / // % ** plus sqrt, log, sin, cos, tan, "
        "factorial, abs, round, min, max, floor, ceil, gcd, and the constants pi "
        "and e. It cannot solve equations or handle variables — substitute the "
        "numbers first and ask for the arithmetic."
    )
    args_model: ClassVar[type[BaseModel]] = CalculatorArgs

    async def run(self, args: CalculatorArgs, context: ToolContext) -> dict:
        # No database, no student, no room. It is pure arithmetic, and taking
        # nothing from the context is what makes that obvious.
        result = evaluate_expression(args.expression)
        return {"expression": args.expression, "result": result}
