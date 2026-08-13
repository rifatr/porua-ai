"""Uploading study material, and searching it.

## Why processing is synchronous

The obvious alternative is to accept the upload, return `202 Accepted`, and read
the file in a background task. We do not, and the reason is the brief's own
requirement to handle bad input deliberately.

Extraction is where most bad input is *discovered*: a password-protected PDF, a
scan with no text layer, a `.pptx` that is really a Word file. In the background
none of those can be answered — the request has already returned `202`, so every
one becomes a status field the client must poll for and then interpret. Done here,
each is an HTTP status with a code: `415`, `413`, `422` with `NO_TEXT_LAYER`.

The cost is a request that takes a second or two, bounded by a 20 MB limit. That
is a good trade at this size and a bad one at 500 MB, which is the honest shape of
the decision: it is right for the constraints, not right forever. When it stops
being right, `_read` and `_index` move into a worker unchanged and the model gains
a status column. Nothing else here changes.

## Why re-uploading the same file is not an error

Students re-upload. They lose the tab, they are not sure it worked, they try
again. A `409 Conflict` for that would be technically defensible and useless. So
the SHA-256 of the contents is the identity: the same bytes in the same room
return the document that already exists, and nothing is re-processed. The client
cannot tell the difference, which is the point.
"""

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import NotFoundError
from app.config import get_settings
from app.documents.chunking import Chunk, chunk_pages
from app.documents.detect import DocumentKind, detect
from app.documents.extract import Page, extract
from app.models.document import SEARCH_CONFIG, Document, DocumentChunk
from app.models.room import Room
from app.models.student import Student
from app.services import room as room_service

logger = logging.getLogger(__name__)

# Chunks returned by one search. Small on purpose: these go into the model's
# prompt, and ten near-identical paragraphs crowd out the answer they are meant
# to support.
DEFAULT_SEARCH_LIMIT = 5
MAX_SEARCH_LIMIT = 10

# Words used from a query on the broad pass. A cap so a model that sends a whole
# paragraph produces a bounded tsquery rather than one OR term per word.
MAX_QUERY_TERMS = 12


@dataclass(frozen=True)
class SearchHit:
    document_id: UUID
    filename: str
    page_no: int | None
    text: str

    @property
    def citation(self) -> str:
        """How the tutor should refer to this passage.

        A Word file has no page number to give — see `app/documents/extract.py` —
        so its citation is the filename alone rather than an invented page.
        """
        return f"{self.filename}, page {self.page_no}" if self.page_no else self.filename


async def create_document(
    db: AsyncSession,
    student: Student,
    room_id: UUID,
    *,
    filename: str,
    data: bytes,
) -> Document:
    """Read a file into the room, or refuse it with a typed reason."""
    room = await room_service.get_room(db, student, room_id)
    settings = get_settings()

    detected = detect(filename, data, max_bytes=settings.max_upload_bytes)
    digest = hashlib.sha256(data).hexdigest()

    existing = await _find_by_digest(db, room.id, digest)
    if existing is not None:
        logger.info("document %s re-uploaded to room %s, reusing", digest[:12], room.id)
        return existing

    # In a worker thread, because this is the one genuinely CPU-bound thing the
    # app does. Measured: a 4.4 MB text-heavy PDF takes ~3 seconds to parse, and
    # a 20 MB one around 14. Run inline it would hold the event loop for that
    # whole time and every other request — every other student — would wait
    # behind it. `await` here yields the loop instead.
    #
    # Note this is not the tool-timeout problem in reverse: there the work was
    # ours to bound with size limits, here it is a library's and cannot be
    # interrupted, so it is moved off the loop rather than cancelled.
    pages, chunks = await asyncio.to_thread(_read, detected.kind, data)

    document = Document(
        room_id=room.id,
        filename=detected.filename,
        kind=detected.kind.value,
        size_bytes=detected.size_bytes,
        sha256=digest,
        # Numbered pages only. A Word file reports None rather than 1, because
        # "one page" would be a claim about the document that is not true.
        page_count=max((p.number for p in pages if p.number), default=None),
        chunk_count=len(chunks),
        chunks=[
            DocumentChunk(chunk_no=chunk.chunk_no, page_no=chunk.page_no, text=chunk.text)
            for chunk in chunks
        ],
    )
    db.add(document)
    await db.flush()
    await db.refresh(document)

    logger.info(
        "indexed %s (%s, %d chunks) into room %s",
        detected.filename,
        detected.kind.value,
        len(chunks),
        room.id,
    )
    return document


async def list_documents(db: AsyncSession, student: Student, room_id: UUID) -> list[Document]:
    """Everything uploaded to this room, newest first."""
    room = await room_service.get_room(db, student, room_id)
    stmt = (
        select(Document)
        .where(Document.room_id == room.id)
        .order_by(Document.created_at.desc(), Document.id)
    )
    return list((await db.execute(stmt)).scalars().all())


async def delete_document(db: AsyncSession, student: Student, document_id: UUID) -> None:
    """Remove a document and its chunks.

    A hard delete, unlike a room. A room is archived because its turns are the
    student's study history and deleting them would punch a hole in it. A
    document is source material with nothing hanging off it, and the usual reason
    to delete one is uploading the wrong file — for which leaving it in place,
    still matching searches, would be the wrong behaviour.
    """
    document = await _owned_document(db, student, document_id)
    # The chunks go with it through ON DELETE CASCADE, so this is one statement
    # rather than a load-then-delete of every chunk through the ORM.
    await db.execute(delete(Document).where(Document.id == document.id))
    await db.flush()


async def search(
    db: AsyncSession,
    room: Room,
    query: str,
    *,
    limit: int = DEFAULT_SEARCH_LIMIT,
) -> list[SearchHit]:
    """Full-text search over one room's uploaded material.

    Scoped to the room by a join, not by a filter the caller passes — so there is
    no argument anyone, including the model, could set to read another room's
    files.
    """
    text = query.strip()
    if not text:
        return []

    limit = min(limit, MAX_SEARCH_LIMIT)

    # Every term must appear. Precise, and it respects quoted phrases and
    # `-excluded` terms, so it is the right first attempt.
    hits = await _run(db, room, func.websearch_to_tsquery(SEARCH_CONFIG, text), limit)
    if hits:
        return hits

    # Nothing matched every term, so try for any of them. This is not a nicety —
    # the query is written by a language model, in natural language, and
    # `websearch_to_tsquery` joins terms with AND:
    #
    #     'protections prevent arbitrary tool calls'
    #       -> 'protect' & 'prevent' & 'arbitrari' & 'tool' & 'call'
    #
    # The document said "protections" and "arbitrary" but never "prevent", so one
    # ordinary English word the model chose reduced five good matches to none.
    # Strict-then-broad keeps the precision when the terms really are all there
    # and degrades to ranked relevance when they are not, instead of degrading to
    # silence — which the model reads as "your materials do not cover this" and
    # answers from general knowledge, confidently and without your handout.
    relaxed = _any_term_query(text)
    return await _run(db, room, relaxed, limit) if relaxed is not None else []


def _any_term_query(text: str):
    """`a | b | c` — matches a chunk containing any of the words.

    Each word goes through `plainto_tsquery` individually rather than being
    pasted into a query string, so nothing the model writes can be operator
    syntax. Stop words reduce to an empty tsquery and drop out harmlessly.
    """
    terms = re.findall(r"\w+", text, flags=re.UNICODE)[:MAX_QUERY_TERMS]
    if not terms:
        return None

    combined = func.plainto_tsquery(SEARCH_CONFIG, terms[0])
    for term in terms[1:]:
        combined = combined.op("||")(func.plainto_tsquery(SEARCH_CONFIG, term))
    return combined


async def _run(db: AsyncSession, room: Room, tsquery, limit: int) -> list[SearchHit]:
    stmt = (
        select(
            Document.id,
            Document.filename,
            DocumentChunk.page_no,
            DocumentChunk.text,
        )
        .select_from(DocumentChunk)
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(Document.room_id == room.id, DocumentChunk.tsv.op("@@")(tsquery))
        # ts_rank scores on how often the terms appear and how close together, so
        # on the broad pass a chunk matching four of five words outranks one
        # matching a single common word. chunk_no breaks ties in document order,
        # so equal scores come back the same way every time instead of however
        # the planner felt like.
        .order_by(func.ts_rank(DocumentChunk.tsv, tsquery).desc(), DocumentChunk.chunk_no)
        .limit(limit)
    )
    return [
        SearchHit(
            document_id=row.id,
            filename=row.filename,
            page_no=row.page_no,
            text=row.text,
        )
        for row in (await db.execute(stmt)).all()
    ]


def _read(kind: DocumentKind, data: bytes) -> tuple[list[Page], list[Chunk]]:
    """Parse and chunk. Pure CPU, no database, so it is safe in a worker thread.

    Kept as one function rather than two `to_thread` calls: chunking is fast, and
    hopping back to the event loop between them would buy nothing.
    """
    pages = extract(kind, data)
    return pages, chunk_pages(pages)


async def _find_by_digest(db: AsyncSession, room_id: UUID, digest: str) -> Document | None:
    stmt = select(Document).where(Document.room_id == room_id, Document.sha256 == digest)
    return (await db.execute(stmt)).scalar_one_or_none()


async def _owned_document(db: AsyncSession, student: Student, document_id: UUID) -> Document:
    """One document, if it belongs to a room belonging to this student.

    The join to rooms is the authorisation. Someone else's document is a 404 for
    the same reason someone else's room is: a 403 would confirm the id exists.
    """
    stmt = (
        select(Document)
        .join(Room, Room.id == Document.room_id)
        .where(Document.id == document_id, Room.student_id == student.id)
    )
    document = (await db.execute(stmt)).scalar_one_or_none()
    if document is None:
        raise NotFoundError(f"No document with id {document_id}.")
    return document
