"""create students and rooms

The first slice of the schema: who is asking, and what topic they are asking
about. Turns, documents and skills arrive in later migrations, each alongside the
feature that needs them.

Revision ID: fab6529a9512
Revises:
Created: 2026-08-10 19:13:14.515621
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "fab6529a9512"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "students",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        # Free text: "Class 8", "HSC 1st year", "University, 2nd year", "AP".
        # No CHECK constraint on purpose — nothing filters on this column, it is
        # prompt input, and a closed set would have no valid value for a
        # university student. NOT NULL because every tutor prompt reads it.
        sa.Column("education_level", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_students")),
    )

    op.create_table(
        "rooms",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("student_id", sa.UUID(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        # NULL means active. Soft delete, so archiving a room never destroys the
        # turns and documents the study-history feature reads.
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_activity_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Deleting a student removes their rooms, and through them their turns,
        # messages and uploaded files.
        sa.ForeignKeyConstraint(
            ["student_id"],
            ["students.id"],
            name=op.f("fk_rooms_student_id_students"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rooms")),
    )

    # Serves "list this student's active rooms, most recently used first".
    #
    # student_id first because we filter on it; last_activity_at second and
    # descending because we sort on it. The wrong order would force Postgres to
    # sort the whole result instead of walking the index.
    #
    # Partial (WHERE archived_at IS NULL) because the list never asks for
    # archived rooms. They never enter the index, so it stays small however many
    # rooms a student retires over the years.
    op.create_index(
        "ix_rooms_student_active",
        "rooms",
        ["student_id", sa.literal_column("last_activity_at DESC")],
        unique=False,
        postgresql_where=sa.text("archived_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_rooms_student_active", table_name="rooms")
    op.drop_table("rooms")
    op.drop_table("students")
