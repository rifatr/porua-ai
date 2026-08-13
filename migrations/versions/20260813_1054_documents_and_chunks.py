"""documents and chunks

Two new tables and nothing else — no data migration, no constraint whose SQL
changed — which is the case autogenerate handles well. It was still read line by
line, and two things were corrected:

- the check constraint was renamed. Our naming convention is
  `ck_%(table_name)s_%(constraint_name)s`, so a constraint called
  `documents_have_something_to_search` came out as
  `ck_documents_documents_have_something_to_search`. The table name belongs to the
  convention, not to the name we write.
- `tsv` is a **generated** column. Autogenerate got it right, which is worth
  checking rather than assuming: had it emitted a plain column, every row would
  have been inserted with a NULL tsvector and search would have matched nothing
  while looking perfectly healthy.

The GIN index on `tsv` is what makes search a lookup instead of a scan. It is
created after the table rather than inside it because `postgresql_using` has no
equivalent inside `create_table`.

Revision ID: 0f6a10cd796d
Revises: dc3397504a0c
Created: 2026-08-13 10:54:30.988558
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0f6a10cd796d"
down_revision: str | None = "dc3397504a0c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("room_id", sa.UUID(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "chunk_count > 0",
            name=op.f("ck_documents_have_something_to_search"),
        ),
        sa.ForeignKeyConstraint(
            ["room_id"],
            ["rooms.id"],
            name=op.f("fk_documents_room_id_rooms"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
        sa.UniqueConstraint("room_id", "sha256", name="uq_documents_room_sha256"),
    )
    op.create_index(
        "ix_documents_room_created",
        "documents",
        ["room_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "document_chunks",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("chunk_no", sa.Integer(), nullable=False),
        sa.Column("page_no", sa.Integer(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', text)", persisted=True),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_document_chunks_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_chunks")),
        sa.UniqueConstraint("document_id", "chunk_no", name="uq_chunks_document_chunk_no"),
    )
    op.create_index(
        "ix_chunks_tsv",
        "document_chunks",
        ["tsv"],
        unique=False,
        postgresql_using="gin",
    )


def downgrade() -> None:
    # Always implement this properly. A migration that cannot be reversed cannot
    # be tested, and `alembic downgrade base` is part of our test suite.
    #
    # Chunks first: they hold the foreign key, so dropping documents first would
    # fail on it.
    op.drop_index("ix_chunks_tsv", table_name="document_chunks", postgresql_using="gin")
    op.drop_table("document_chunks")
    op.drop_index("ix_documents_room_created", table_name="documents")
    op.drop_table("documents")
