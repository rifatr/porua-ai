"""One rejected thing about one model response.

A failure has to serve three audiences at once, which is why it is a record and
not a string:

1. **The model**, in the repair prompt. It needs `detail` — a specific, actionable
   sentence. "Invalid response" produces another invalid response.
2. **A developer**, through `GET /turns/{id}`. The list is stored on the attempt
   row as JSON, so the inspection endpoint shows exactly why an answer was thrown
   away — the question the brief asks that endpoint to answer.
3. **Code**, later. `code` is stable and machine-readable, so counting how often
   the model produces duplicate concepts is a query rather than a text search.
"""

import enum
from dataclasses import dataclass


class Layer(enum.StrEnum):
    """Which check rejected the response. Ordered by when it runs."""

    PARSE = "parse"
    SCHEMA = "schema"
    CONTENT = "content"


@dataclass(frozen=True)
class ValidationFailure:
    layer: Layer
    code: str
    detail: str
    field: str | None = None

    def as_dict(self) -> dict:
        """The stored form, written to turn_attempts.validation_failures."""
        return {
            "layer": str(self.layer),
            "code": self.code,
            "detail": self.detail,
            "field": self.field,
        }

    def as_instruction(self) -> str:
        """The form the repair prompt shows the model."""
        where = f" (field: {self.field})" if self.field else ""
        return f"{self.code}{where}: {self.detail}"


class ResponseInvalid(Exception):
    """The response cannot be used as it stands.

    Carries every failure found by the layer that rejected it — all of them, not
    just the first. A model told about one problem at a time takes one billable
    call per problem; told about four at once it usually fixes all four.
    """

    def __init__(self, failures: list[ValidationFailure]) -> None:
        if not failures:  # pragma: no cover - guards a programming mistake
            raise ValueError("ResponseInvalid needs at least one failure")
        self.failures = failures
        self.layer = failures[0].layer
        super().__init__("; ".join(f.as_instruction() for f in failures))
