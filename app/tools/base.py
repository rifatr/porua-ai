"""What a tool is, and what it is allowed to know.

The brief's requirement is short and sharp: *"Tool use must be controlled; the
model should not call arbitrary code or call tools indefinitely."* Two of the five
protections that answer it live in this file, and they live here rather than in the
loop because they should be impossible to forget.

**Identity comes from `ToolContext`, never from arguments.** A tool receives the
student as part of its context, which was resolved from the request header long
before the model saw anything. No tool takes a `student_id` argument, so there is
no way for the model to ask for another student's data — the parameter does not
exist for it to fill in. This is why `args_model` and `ToolContext` are separate
types rather than one bag of values.

**Arguments are a Pydantic model, always.** `run` is handed a validated object, so
a tool never parses model output itself. The declaration sent to the provider is
generated from that same class, so what the model is told and what is enforced
cannot drift apart.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.base import ToolSpec
from app.models.room import Room
from app.models.student import Student
from app.models.tool_call import ToolStatus

__all__ = ["Tool", "ToolContext", "ToolFailure", "ToolStatus"]


@dataclass(frozen=True)
class ToolContext:
    """Everything a tool may know, and nothing it may not.

    Note what is absent: the model's raw output, the prompt, the turn. A tool
    answers a question about *this student in this room* and cannot reach further.
    """

    db: AsyncSession
    student: Student
    room: Room


class ToolFailure(Exception):
    """A tool refusing a request it understood.

    Distinct from a crash. `detail` is written to be read by the model, because it
    is sent back as the tool's result — "unknown function 'foo'" tells it what to
    do next, "ValueError" does not.
    """

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class Tool(ABC):
    """One thing the model can ask us to do."""

    #: The name the model uses. Must match the declaration exactly.
    name: ClassVar[str]
    #: Shown to the model. This is prompt engineering — it is the only thing the
    #: model reads when deciding whether this tool is the right one, so it should
    #: say when *not* to use it as well as when to.
    description: ClassVar[str]
    #: Arguments, as a flat Pydantic model. Flat on purpose: nested models produce
    #: `$defs` references that provider schemas handle inconsistently.
    args_model: ClassVar[type[BaseModel]]

    @abstractmethod
    async def run(self, args: BaseModel, context: ToolContext) -> dict:
        """Do the work. Returns JSON-serialisable data sent back to the model.

        Raise `ToolFailure` for a request that cannot be honoured. Anything else
        raised is a bug, and the loop records it as `failed` rather than letting it
        break the turn.
        """

    @classmethod
    def spec(cls) -> ToolSpec:
        """The declaration sent to the provider, generated from `args_model`."""
        return ToolSpec(
            name=cls.name,
            description=cls.description,
            parameters=cls.args_model.model_json_schema(),
        )
