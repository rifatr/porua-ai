"""skill runs on turns

A skill run **is a turn**. `skill_runs.turn_id` is `UNIQUE NOT NULL`, so a turn
has at most one run and a run always belongs to one turn.

That is the whole point of this table's shape. The model calls a skill makes are
`turn_attempts` rows with `purpose='skill'`, alongside the tutor's — so
`GET /turns/{id}` inspects a quiz in the same detail as a conversation, the room
timeline contains the quiz where the student asked for it, and study history
counts it as work done. An earlier version of this migration hung `skill_runs` off
`rooms` with its attempts in a JSONB column; it was reversed before being
committed, because it left the project with two audit trails of different shapes.

The two indexes at the bottom are why a quiz gets tables rather than a JSONB blob.
The brief names four ways a quiz goes wrong, and these make two of them
**impossible to store**:

    ux_choice_text     (question_id, lower(btrim(text)))  UNIQUE
    ux_choice_correct  (question_id) WHERE is_correct     UNIQUE

The first means two options in a question cannot say the same thing, with
`"Paris"`, `" paris "` and `"PARIS"` counted as one answer — which is how a
student reads them. The second means a question cannot have two right answers; the
partial `WHERE` is what makes it work, since without it a question could hold only
one *wrong* option too.

Autogenerate produced both correctly, including the functional expression, which
was worth checking rather than assuming — a plain index on `text` would look
almost identical and enforce nothing about case or spacing.

Revision ID: 7882dd12e343
Revises: 0f6a10cd796d
Created: 2026-08-14 16:34:57.768390
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "7882dd12e343"
down_revision: str | None = "0f6a10cd796d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "skill_runs",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("turn_id", sa.UUID(), nullable=False),
        sa.Column("skill_name", sa.String(length=64), nullable=False),
        sa.Column("skill_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("request", JSONB, nullable=False),
        sa.Column("result", JSONB, nullable=True),
        sa.Column("checks", JSONB, nullable=True),
        sa.Column("failure_reason", sa.String(length=64), nullable=True),
        sa.Column("failure_detail", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('succeeded', 'failed')",
            name=op.f("ck_skill_runs_status_is_valid"),
        ),
        sa.CheckConstraint(
            "(status = 'succeeded' AND failure_reason IS NULL) OR "
            "(status = 'failed' AND failure_reason IS NOT NULL)",
            name=op.f("ck_skill_runs_failures_explain_themselves"),
        ),
        sa.ForeignKeyConstraint(
            ["turn_id"],
            ["turns.id"],
            name=op.f("fk_skill_runs_turn_id_turns"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_skill_runs")),
        # One run per turn. Also the index the inspection endpoint looks it up by,
        # so no separate index is needed.
        sa.UniqueConstraint("turn_id", name=op.f("uq_skill_runs_turn_id")),
    )

    op.create_table(
        "quiz_questions",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("stem", sa.Text(), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=300), nullable=True),
        sa.CheckConstraint(
            "position >= 0", name=op.f("ck_quiz_questions_position_is_not_negative")
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["skill_runs.id"],
            name=op.f("fk_quiz_questions_run_id_skill_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_quiz_questions")),
        sa.UniqueConstraint("run_id", "position", name="uq_questions_run_position"),
    )

    op.create_table(
        "quiz_choices",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("question_id", sa.UUID(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("is_correct", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "position >= 0", name=op.f("ck_quiz_choices_position_is_not_negative")
        ),
        sa.ForeignKeyConstraint(
            ["question_id"],
            ["quiz_questions.id"],
            name=op.f("fk_quiz_choices_question_id_quiz_questions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_quiz_choices")),
        sa.UniqueConstraint(
            "question_id", "position", name="uq_choices_question_position"
        ),
    )

    # The two rules the database owns. See the module docstring.
    op.create_index(
        "ux_choice_correct",
        "quiz_choices",
        ["question_id"],
        unique=True,
        postgresql_where=sa.text("is_correct"),
    )
    op.create_index(
        "ux_choice_text",
        "quiz_choices",
        ["question_id", sa.literal_column("lower(btrim(text))")],
        unique=True,
    )


def downgrade() -> None:
    # Always implement this properly. A migration that cannot be reversed cannot
    # be tested, and `alembic downgrade base` is part of our test suite.
    #
    # Children first: each table below holds the foreign key into the one after
    # it, so the other order fails on the constraint.
    op.drop_index("ux_choice_text", table_name="quiz_choices")
    op.drop_index(
        "ux_choice_correct",
        table_name="quiz_choices",
        postgresql_where=sa.text("is_correct"),
    )
    op.drop_table("quiz_choices")
    op.drop_table("quiz_questions")
    op.drop_table("skill_runs")
