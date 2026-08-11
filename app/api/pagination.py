"""Cursor paging (also called keyset paging).

Why not `?page=50`:
    Page numbers become SQL OFFSET. To return rows 5000-5020 Postgres must walk
    past the first 5000 rows and throw them away. That gets slower every week as
    history grows, which is exactly the property the brief asks us about.

What we do instead:
    Sort by (sort_column DESC, id DESC) and remember where we stopped. The next
    request says "give me rows after this exact point", which is one index seek no
    matter how much history exists.

The id is part of the sort key on purpose. Two rows can share a timestamp; without
a tie-breaker the same row could appear on two pages, or be skipped entirely.
"""

import base64
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import ColumnElement, Select, tuple_

from app.api.errors import ValidationFailedError


@dataclass(frozen=True)
class Cursor:
    """The position of the last row on the previous page.

    The sort value is a timestamp for some lists (rooms, by last activity) and an
    integer for others (turns, by sequence number). The encoded cursor records
    which, so decoding restores the right type instead of guessing.
    """

    sort_value: datetime | int
    id: UUID

    def encode(self) -> str:
        if isinstance(self.sort_value, datetime):
            kind, value = "dt", self.sort_value.isoformat()
        else:
            kind, value = "n", int(self.sort_value)
        raw = json.dumps({"t": kind, "v": value, "id": str(self.id)})
        return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, value: str) -> "Cursor":
        try:
            padded = value + "=" * (-len(value) % 4)
            data = json.loads(base64.urlsafe_b64decode(padded).decode())
            sort_value: datetime | int = (
                datetime.fromisoformat(data["v"]) if data["t"] == "dt" else int(data["v"])
            )
            return cls(sort_value=sort_value, id=UUID(data["id"]))
        except Exception as exc:  # malformed cursors are user error, not a crash
            raise ValidationFailedError("The cursor parameter is not valid.") from exc


class Page[T](BaseModel):
    """A page of results plus the cursor for the next one.

    `next_cursor` is null when there are no more rows, which is how a client
    knows to stop.
    """

    items: list[T]
    next_cursor: str | None = None


def apply_cursor(
    stmt: Select,
    *,
    sort_column: ColumnElement[Any],
    id_column: ColumnElement[Any],
    cursor: str | None,
    limit: int,
) -> Select:
    """Add ordering, the cursor filter, and the limit to a query.

    We fetch limit + 1 rows. If the extra row comes back we know there is another
    page, without running a second COUNT query over the whole table.
    """
    stmt = stmt.order_by(sort_column.desc(), id_column.desc())
    if cursor:
        pos = Cursor.decode(cursor)
        # Row-value comparison. Postgres can use the composite index for this,
        # which a chain of OR conditions would not do as cleanly.
        stmt = stmt.where(tuple_(sort_column, id_column) < tuple_(pos.sort_value, pos.id))
    return stmt.limit(limit + 1)


def build_page(
    rows: list[Any],
    *,
    limit: int,
    sort_attr: str,
    serializer: Any,
) -> Page:
    """Trim the extra row and turn the last remaining one into a cursor."""
    has_more = len(rows) > limit
    visible = rows[:limit]
    next_cursor = None
    if has_more and visible:
        last = visible[-1]
        next_cursor = Cursor(sort_value=getattr(last, sort_attr), id=last.id).encode()
    return Page(items=[serializer.model_validate(r) for r in visible], next_cursor=next_cursor)
