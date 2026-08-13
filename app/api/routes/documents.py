"""Document endpoints.

Two routers, because the paths have two different shapes: uploading and listing
belong to a room, deleting belongs to the document itself. FastAPI takes one
prefix per router, so this is what "nested under a room" costs.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Response, UploadFile, status

from app.api.deps import CurrentStudent, DbSession
from app.config import get_settings
from app.documents.detect import supported_extensions
from app.schemas.document import DocumentRead, DocumentUploadRead
from app.services import document as document_service

rooms_router = APIRouter(prefix="/rooms", tags=["documents"])
documents_router = APIRouter(prefix="/documents", tags=["documents"])

_EXTENSIONS = ", ".join(f"`{extension}`" for extension in supported_extensions())


@rooms_router.post(
    "/{room_id}/documents",
    response_model=DocumentUploadRead,
    status_code=status.HTTP_201_CREATED,
    summary="Upload study material",
    description=(
        f"Accepts {_EXTENSIONS}. The text is read out, cut into searchable "
        "pieces, and indexed before this returns — so a `201` means the tutor can "
        "already search it, and a failure tells you exactly what was wrong "
        "instead of a status you have to poll for.\n\n"
        "The file type is decided from the **contents**, not the filename, so a "
        "`.txt` renamed to `.pdf` is rejected.\n\n"
        "Uploading the same file twice returns the document that already exists "
        "rather than indexing it again — the SHA-256 of the contents is its "
        "identity.\n\n"
        f"A room keeps **{document_service.MAX_DOCUMENTS_PER_ROOM} files**. "
        "Uploading past that is not refused — the oldest is removed instead, and "
        "named in `evicted`, so a student mid-study is never blocked by a file "
        "they added last term.\n\n"
        "**Failures**\n\n"
        "| Status | `code` | Meaning |\n"
        "|---|---|---|\n"
        "| 415 | `UNSUPPORTED_TYPE` | Not a supported type, or contents do not match the name |\n"
        "| 413 | `FILE_TOO_LARGE` | Over the 20 MB limit |\n"
        "| 422 | `CORRUPT_ARCHIVE` | Damaged, or not really the format it claims |\n"
        "| 422 | `ENCRYPTED` | Password-protected |\n"
        "| 422 | `NO_TEXT_LAYER` | A scan — images of text, which we do not OCR |\n"
        "| 422 | `UNDECODABLE_TEXT` | Fonts carry no character map, so the text reads as glyph "
        "numbers |\n"
        "| 422 | `EMPTY_EXTRACTION` | Opens fine, contains no text |\n"
    ),
)
async def upload_document(
    room_id: UUID,
    db: DbSession,
    student: CurrentStudent,
    file: Annotated[UploadFile, File(description="The document to index. Supported file types: pdf, pptx and docx.")],
) -> DocumentRead:
    # Read at most one byte past the limit. The body has already been received by
    # this point — bounding it for real needs a limit on the server or proxy in
    # front — but this keeps an oversized upload from being held in memory while
    # we work out that we are going to reject it.
    data = await file.read(get_settings().max_upload_bytes + 1)

    result = await document_service.create_document(
        db,
        student,
        room_id,
        filename=file.filename or "",
        data=data,
    )
    return DocumentUploadRead(
        **DocumentRead.model_validate(result.document).model_dump(),
        evicted=result.evicted,
    )


@rooms_router.get(
    "/{room_id}/documents",
    response_model=list[DocumentRead],
    summary="What has been uploaded to this room",
    description=(
        "Newest first. Not paged: a room holds a handful of files, and a cursor "
        "would be ceremony over a list that fits on one screen."
    ),
)
async def list_documents(
    room_id: UUID, db: DbSession, student: CurrentStudent
) -> list[DocumentRead]:
    documents = await document_service.list_documents(db, student, room_id)
    return [DocumentRead.model_validate(document) for document in documents]


@documents_router.delete(
    "/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a document",
    description=(
        "Removes the document and everything indexed from it, so the tutor stops "
        "finding it immediately. Unlike a room this is a real delete — source "
        "material has no study history hanging off it, and the usual reason to "
        "delete one is having uploaded the wrong file."
    ),
)
async def delete_document(
    document_id: UUID, db: DbSession, student: CurrentStudent
) -> Response:
    await document_service.delete_document(db, student, document_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
