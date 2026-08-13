"""tool calls

One row every time the model asked for a tool, including the times it asked for a
tool that does not exist or passed arguments that did not fit.

Autogenerate produced this one correctly, which is worth noting because it usually
does not: a brand new table with no data migration and no *changed* constraint is
the case it handles well. The things it reliably misses — a check constraint whose
SQL changed, a backfill, a downgrade that restores a previous shape — are all
absent here. It was still read line by line before being kept, and one thing was
removed: an index on `(attempt_id, call_no)` that duplicated the UNIQUE constraint
on the same columns.

Revision ID: dc3397504a0c
Revises: e1fe1d77cfbf
Created: 2026-08-13 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "dc3397504a0c"
down_revision: str | None = "e1fe1d77cfbf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tool_calls",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("attempt_id", sa.UUID(), nullable=False),
        sa.Column("call_no", sa.Integer(), nullable=False),
        sa.Column("tool_name", sa.String(length=64), nullable=False),
        sa.Column(
            "arguments",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["turn_attempts.id"],
            name=op.f("fk_tool_calls_attempt_id_turn_attempts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tool_calls")),
        # Also the index that serves "every call for this attempt, in order".
        # Postgres builds a btree for a UNIQUE constraint, so a separate index on
        # the same two columns would cost a write per insert and buy nothing.
        sa.UniqueConstraint(
            "attempt_id", "call_no", name="uq_tool_calls_attempt_id_call_no"
        ),
        sa.CheckConstraint(
            "status IN ('ok', 'unknown_tool', 'invalid_arguments', 'failed', 'timed_out')",
            name=op.f("ck_tool_calls_status_is_valid"),
        ),
        sa.CheckConstraint("call_no > 0", name=op.f("ck_tool_calls_call_no_is_positive")),
        sa.CheckConstraint(
            "(status = 'ok' AND result IS NOT NULL)"
            " OR (status <> 'ok' AND error_detail IS NOT NULL)",
            name=op.f("ck_tool_calls_tool_calls_explain_themselves"),
        ),
    )


def downgrade() -> None:
    # Always implement this properly. A migration that cannot be reversed cannot
    # be tested, and `alembic downgrade base` is part of our test suite.
    op.drop_table("tool_calls")
