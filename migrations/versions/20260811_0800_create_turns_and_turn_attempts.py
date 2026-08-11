"""create turns and turn attempts

Slice S2. A turn is one question and answer inside a room; turn_attempts records
every call made to Gemini while producing it, including the calls that failed.

Revision ID: 85fb66f33dce
Revises: fab6529a9512
Created: 2026-08-11 08:00:57.018181
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "85fb66f33dce"
down_revision: str | None = "fab6529a9512"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "turns",
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("room_id", sa.UUID(), nullable=False),
        # Position within the room, from 1. A stable order that does not depend on
        # timestamps, which tie when two turns land in the same transaction.
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("student_message", sa.Text(), nullable=False),
        # NULL until the turn succeeds. A failed turn never carries an answer.
        sa.Column("answer_text", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("failure_reason", sa.String(length=64), nullable=True),
        # created_at is the start, completed_at the end. No separate started_at:
        # now() is the transaction time and a request creates exactly one turn,
        # so the two would be the same fact stored twice.
        #
        # No updated_at either — completed_at already records the last write, and
        # more precisely. And no stored duration: it is arithmetic on these two
        # columns, so it is computed on read.
        #
        # Token counts deliberately live on turn_attempts only.
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["room_id"],
            ["rooms.id"],
            name=op.f("fk_turns_room_id_rooms"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_turns")),
        # Turn numbers cannot fork if two requests for the same room race.
        sa.UniqueConstraint("room_id", "seq", name="uq_turns_room_id_seq"),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name=op.f("ck_turns_status_is_valid"),
        ),
        sa.CheckConstraint("seq > 0", name=op.f("ck_turns_seq_is_positive")),
        # An answer exists only on success, and a failure always says why. This is
        # the schema-level version of the project's central rule: a turn either
        # produces a checked result or it fails visibly, never something in
        # between.
        sa.CheckConstraint(
            "(status = 'succeeded' AND answer_text IS NOT NULL)"
            " OR (status = 'failed' AND failure_reason IS NOT NULL)"
            " OR status = 'running'",
            name=op.f("ck_turns_terminal_states_are_complete"),
        ),
    )

    # The room timeline: newest turn first.
    op.create_index(
        "ix_turns_room_id_seq",
        "turns",
        ["room_id", sa.literal_column("seq DESC")],
        unique=False,
    )

    op.create_table(
        "turn_attempts",
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("turn_id", sa.UUID(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("purpose", sa.String(length=16), nullable=False),
        # Which prompt produced this, exactly. The checksum is what makes the link
        # trustworthy: a prompt edited in place would otherwise silently change
        # the meaning of every attempt already referencing its version.
        sa.Column("prompt_name", sa.String(length=64), nullable=False),
        sa.Column("prompt_version", sa.Integer(), nullable=False),
        sa.Column("prompt_sha256", sa.String(length=64), nullable=False),
        sa.Column("rendered_prompt", sa.Text(), nullable=False),
        sa.Column(
            "request_params",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("raw_response", sa.Text(), nullable=True),
        sa.Column("finish_reason", sa.String(length=32), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("thought_tokens", sa.Integer(), nullable=True),
        sa.Column("error_type", sa.String(length=64), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        # Filled by S3: which content checks this response failed.
        sa.Column("validation_failures", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        # Each attempt stamps its own start from the application clock. It cannot
        # use now() like turns do: now() is the transaction time, and several
        # attempts made while answering one turn would all get an identical
        # value — defeating the one question this table answers, which attempt
        # was slow. Duration is computed on read from these two columns.
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["turn_id"],
            ["turns.id"],
            name=op.f("fk_turn_attempts_turn_id_turns"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_turn_attempts")),
        sa.UniqueConstraint(
            "turn_id", "attempt_no", name="uq_turn_attempts_turn_id_attempt_no"
        ),
        sa.CheckConstraint(
            "purpose IN ('tutor', 'repair', 'skill', 'summarize')",
            name=op.f("ck_turn_attempts_purpose_is_valid"),
        ),
        sa.CheckConstraint(
            "attempt_no > 0", name=op.f("ck_turn_attempts_attempt_no_is_positive")
        ),
    )


def downgrade() -> None:
    # Children before parents.
    op.drop_table("turn_attempts")
    op.drop_index("ix_turns_room_id_seq", table_name="turns")
    op.drop_table("turns")
