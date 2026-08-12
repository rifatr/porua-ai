"""structured tutor output on turns

Adds the two fields the tutor now returns alongside its answer, and tightens the
constraint that defines a complete turn.

Autogenerate produced roughly two thirds of this. Three things it could not:

1. **The changed `terminal_states_are_complete` check.** Alembic compares check
   constraints by name, not by their SQL, so a constraint whose body changed looks
   unchanged. It has to be dropped and recreated by hand — the reason
   `alembic check` is run after every autogenerate rather than trusting the diff.
2. **The backfill.** Existing succeeded turns predate `in_scope` and would violate
   the tightened constraint the moment it was created.
3. **A downgrade that actually reverses.** The old constraint has to come back, or
   downgrading leaves the database in a shape no migration describes.

Revision ID: e1fe1d77cfbf
Revises: 85fb66f33dce
Created: 2026-08-12 10:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e1fe1d77cfbf"
down_revision: str | None = "85fb66f33dce"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The version before this migration: no in_scope clause.
_COMPLETE_V1 = (
    "(status = 'succeeded' AND answer_text IS NOT NULL)"
    " OR (status = 'failed' AND failure_reason IS NOT NULL)"
    " OR status = 'running'"
)

_COMPLETE_V2 = (
    "(status = 'succeeded' AND answer_text IS NOT NULL AND in_scope IS NOT NULL)"
    " OR (status = 'failed' AND failure_reason IS NOT NULL)"
    " OR status = 'running'"
)


def upgrade() -> None:
    op.add_column("turns", sa.Column("in_scope", sa.Boolean(), nullable=True))
    op.add_column(
        "turns",
        sa.Column("concepts", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )

    # Turns answered by tutor_system v1, which returned prose and no verdict. They
    # were answered, so in_scope is true; nothing tagged them, so concepts is
    # empty. Recorded as what we know rather than left NULL, because NULL would
    # fail the tightened constraint below and would also read, wrongly, as "this
    # turn is still running".
    op.execute(
        """
        UPDATE turns
           SET in_scope = true,
               concepts = '[]'::jsonb
         WHERE status = 'succeeded'
           AND in_scope IS NULL
        """
    )

    # Bare names throughout, never the "ck_turns_" form. The naming convention in
    # app/db/base.py expands them, and passing an expanded name gets it expanded a
    # second time into ck_turns_ck_turns_…, which matches nothing.
    op.drop_constraint("terminal_states_are_complete", "turns", type_="check")
    op.create_check_constraint("terminal_states_are_complete", "turns", _COMPLETE_V2)
    op.create_check_constraint(
        "off_topic_turns_teach_nothing",
        "turns",
        "in_scope IS NOT FALSE OR concepts IS NULL OR concepts = '[]'::jsonb",
    )

    op.create_index(
        "ix_turns_concepts",
        "turns",
        ["concepts"],
        unique=False,
        postgresql_using="gin",
        postgresql_where=sa.text("concepts IS NOT NULL"),
    )


def downgrade() -> None:
    # Always implement this properly. A migration that cannot be reversed cannot
    # be tested, and `alembic downgrade base` is part of our test suite.
    op.drop_index(
        "ix_turns_concepts",
        table_name="turns",
        postgresql_using="gin",
        postgresql_where=sa.text("concepts IS NOT NULL"),
    )
    op.drop_constraint("off_topic_turns_teach_nothing", "turns", type_="check")
    op.drop_constraint("terminal_states_are_complete", "turns", type_="check")
    # Put the old rule back. Dropping the columns without this would leave the
    # table with no completeness constraint at all.
    op.create_check_constraint("terminal_states_are_complete", "turns", _COMPLETE_V1)

    op.drop_column("turns", "concepts")
    op.drop_column("turns", "in_scope")
