"""Uploaded study material, and the searchable pieces it was cut into.

A student uploads their class handout, lecture slides or notes into a room. We
read the text out, cut it into chunks, and index those chunks so the tutor can
search them. Two tables, because they answer two different questions: `documents`
is "what did I upload", `document_chunks` is "where does this sentence appear".

## What is not here

**The file itself.** We store its fingerprint, its name and its size, but not its
bytes. Nothing ever reads them back — the tutor searches text, and text is what we
extracted. Keeping the binary would mean a volume or an object store to hold it, a
lifecycle to clean it up, and a second copy of something we already processed. If
"download the original" ever becomes a feature it brings its own storage with it.

**A processing status.** Upload is synchronous, so a row exists only if the file
was read successfully — its existence *is* the status. Bad files never get a row;
they get a typed HTTP error instead, which is more use to the caller than a field
they have to poll for. See `app/services/document.py` for the trade-off in full.
"""

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.schema import Computed

from app.db.base import Base, CreatedAtMixin, UUIDPrimaryKey

if TYPE_CHECKING:
    from app.models.room import Room

# The Postgres text-search configuration used for both indexing and querying.
# The two must match — a `tsvector` built with `english` and a `tsquery` built
# with `simple` will silently fail to match on any stemmed word.
SEARCH_CONFIG = "english"


class Document(UUIDPrimaryKey, CreatedAtMixin, Base):
    __tablename__ = "documents"

    room_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("rooms.id", ondelete="CASCADE"),
        nullable=False,
    )

    filename: Mapped[str] = mapped_column(String(255), nullable=False)

    # "pdf" / "docx" / "pptx". Decided from the bytes, not from the name the
    # browser sent, so this records what the file actually was.
    kind: Mapped[str] = mapped_column(String(16), nullable=False)

    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)

    # Fingerprint of the file contents. Paired with room_id in a UNIQUE
    # constraint so re-uploading the same file is recognised rather than
    # processed and indexed a second time.
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    # Pages for a PDF, slides for a PowerPoint. NULL for Word — see
    # `app/documents/extract.py` for why a .docx has no page numbers to record.
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False)

    room: Mapped["Room"] = relationship()
    chunks: Mapped[list["DocumentChunk"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        # Re-uploading the same file into the same room is a no-op rather than a
        # duplicate. Scoped to the room, not global: two students uploading the
        # same textbook each get their own copy, and one deleting it must not
        # affect the other.
        UniqueConstraint("room_id", "sha256", name="uq_documents_room_sha256"),
        # A document that produced no chunks has nothing to search and should
        # never have been saved. Extraction rejects that case; this makes it
        # impossible to store even if a future code path forgets to.
        CheckConstraint("chunk_count > 0", name="have_something_to_search"),
        # Serves the room's document list, newest first.
        Index("ix_documents_room_created", "room_id", "created_at"),
    )


class DocumentChunk(UUIDPrimaryKey, Base):
    """One searchable piece of a document, with the page it came from."""

    __tablename__ = "document_chunks"

    document_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Position within the document, from 1. Gives search results a stable order
    # and lets a caller ask for the chunk before or after a hit.
    chunk_no: Mapped[int] = mapped_column(Integer, nullable=False)

    # The page or slide this text came from. NULL for Word files, which do not
    # contain page numbers at all.
    page_no: Mapped[int | None] = mapped_column(Integer, nullable=True)

    text: Mapped[str] = mapped_column(Text, nullable=False)

    # Maintained by Postgres, not by us. A generated column cannot drift from the
    # text it summarises: there is no code path that updates one without the
    # other, because there is no code path that updates this at all.
    tsv: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(f"to_tsvector('{SEARCH_CONFIG}', text)", persisted=True),
        nullable=False,
    )

    document: Mapped["Document"] = relationship(back_populates="chunks")

    __table_args__ = (
        UniqueConstraint("document_id", "chunk_no", name="uq_chunks_document_chunk_no"),
        # The index the search tool lives on. GIN is the right kind for tsvector:
        # it indexes each word (technically each lexeme) separately, which is what
        # makes "find the chunks containing photosynthesis" a lookup rather than a
        # scan of every chunk in the database.
        Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),
    )
