"""Cursor encoding and decoding.

Small, but every list endpoint depends on it, so it gets its own tests.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.api.errors import ValidationFailedError
from app.api.pagination import Cursor


def test_cursor_survives_a_round_trip() -> None:
    original = Cursor(sort_value=datetime(2026, 8, 11, 9, 30, tzinfo=UTC), id=uuid4())
    restored = Cursor.decode(original.encode())
    assert restored == original


def test_encoded_cursor_is_url_safe() -> None:
    encoded = Cursor(sort_value=datetime.now(UTC), id=uuid4()).encode()
    assert "+" not in encoded and "/" not in encoded and "=" not in encoded


def test_a_bad_cursor_is_a_422_not_a_crash() -> None:
    # Cursors arrive from the outside world, so a broken one is user error.
    with pytest.raises(ValidationFailedError):
        Cursor.decode("not-a-real-cursor")
