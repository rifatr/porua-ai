"""The list of tools that exist. Fixed at import, never built from model output.

This is the first and most important of the five protections, and it works by
being boring: a dictionary, filled in once when the module is imported, and only
ever read afterwards.

## The property this gives us

There is no path from a model response to running code we did not write.

`resolve` looks a name up in a dict and returns `None` if it is missing. There is
no `getattr`, no `importlib`, no `eval`, no naming convention that turns a string
into a callable. If the model asks for `os.system` or `delete_everything`, the
lookup misses and it gets an error message back. Adding a tool means editing this
file — which means it goes through review like any other code.

That is worth stating precisely on the review call, because "we validate the tool
name against a list" sounds similar and is much weaker. A blocklist can be
incomplete. This is not a check that could miss something; there is simply no
mechanism by which an unlisted name could execute.

## Validation happens here too

`validate_arguments` is the only way arguments become a Python object. A tool's
`run` is therefore never handed raw model output — by the time it is called, the
arguments have been through the same Pydantic model that generated the declaration
the model was reading. What we promised and what we enforce are one class.
"""

import logging

from pydantic import BaseModel, ValidationError

from app.llm.base import ToolSpec
from app.tools.base import Tool
from app.tools.calculator import CalculatorTool
from app.tools.study_history import StudyHistoryTool

logger = logging.getLogger(__name__)


class InvalidArguments(Exception):
    """The model's arguments did not fit the tool's model, so it did not run."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


def _build() -> dict[str, Tool]:
    tools: dict[str, Tool] = {}
    for tool_class in (StudyHistoryTool, CalculatorTool):
        instance = tool_class()
        if instance.name in tools:  # pragma: no cover - guards a typo at import
            raise RuntimeError(f"Two tools are both called {instance.name!r}")
        tools[instance.name] = instance
    return tools


#: Built once at import. Tools are stateless, so one instance each is enough.
_TOOLS: dict[str, Tool] = _build()


def resolve(name: str) -> Tool | None:
    """The tool with this name, or None. The only way a name becomes a callable."""
    return _TOOLS.get(name)


def specs() -> tuple[ToolSpec, ...]:
    """Declarations for every tool, to attach to a request."""
    return tuple(tool.spec() for tool in _TOOLS.values())


def names() -> tuple[str, ...]:
    return tuple(_TOOLS)


def validate_arguments(tool: Tool, arguments: dict) -> BaseModel:
    """Turn the model's arguments into a checked object, or refuse.

    The error message is written for the model to read, because it is sent back as
    the tool result. Pydantic's own text names the field and says what was wrong
    with it, which is exactly what a retry needs — so it is passed through rather
    than replaced with something tidier and less useful.
    """
    try:
        return tool.args_model.model_validate(arguments)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'arguments'}: {error['msg']}"
            for error in exc.errors()
        )
        raise InvalidArguments(problems) from exc
