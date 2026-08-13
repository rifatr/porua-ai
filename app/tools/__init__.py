"""Tools the AI tutor can call during a turn.

A tool is for something the model genuinely cannot do: read this student's private
data, or compute a number exactly. Everything else belongs in the prompt. The
four-part test each tool had to pass, and the list of what it rejected, is in
`docs/PLAN.md` §8.

The five protections the brief's "tool use must be controlled" asks for, and where
each one lives:

1. **A fixed list** — `registry.py`. No path from model output to unlisted code.
2. **Arguments validated first** — `registry.validate_arguments`, using the same
   Pydantic model that produced the declaration the model read.
3. **Identity from the request** — `base.ToolContext`. No tool takes a student id,
   so the model cannot ask for another student's data.
4. **Hard limits** — `services/turn.py`: calls per turn, loop iterations, a
   timeout per tool, and the turn deadline over all of it.
5. **Repeat calls answered from cache** — `services/turn.py`. Limits end a loop
   eventually; this ends it immediately.
"""

from app.tools.base import Tool, ToolContext, ToolFailure, ToolStatus
from app.tools.registry import (
    InvalidArguments,
    names,
    resolve,
    specs,
    validate_arguments,
)

__all__ = [
    "InvalidArguments",
    "Tool",
    "ToolContext",
    "ToolFailure",
    "ToolStatus",
    "names",
    "resolve",
    "specs",
    "validate_arguments",
]
