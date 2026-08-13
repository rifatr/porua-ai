"""The document response shapes."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class DocumentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    room_id: UUID
    filename: str
    kind: str = Field(
        description="What the file actually was, decided from its contents rather "
        "than its name: `pdf`, `docx` or `pptx`."
    )
    size_bytes: int
    page_count: int | None = Field(
        description="Pages for a PDF, slides for a PowerPoint. Null for Word — a "
        ".docx does not contain page numbers; they are worked out by whatever "
        "renders it."
    )
    chunk_count: int = Field(
        description="Searchable pieces the text was cut into. The tutor searches "
        "these, and cites the page each one came from."
    )
    created_at: datetime
