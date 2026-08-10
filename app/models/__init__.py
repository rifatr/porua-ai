"""Every model must be imported here.

Alembic autogenerate only sees tables registered on Base.metadata, and a model is
only registered once its module has been imported. Forgetting to add a model here
is the usual reason a new table is missing from a migration.
"""

from app.db.base import Base
from app.models.room import Room
from app.models.student import Student

__all__ = ["Base", "Room", "Student"]
