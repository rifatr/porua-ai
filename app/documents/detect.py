"""What is this file, really?

The brief asks for bad and unsupported input to be handled deliberately, and this
is the first place that happens — before any parser is handed the bytes.

## Why the extension is not enough

A filename is a string the client chose. Renaming `virus.exe` to `notes.pdf`
changes nothing about the contents, and handing those bytes to a PDF parser to
find out is exactly the wrong order. So we check both:

    the extension says what the file claims to be
    the first few bytes say what it is

Disagreement is a rejection. That catches the honest mistake (a `.doc` saved as
`.docx`) and the dishonest one with the same code path.

## Why not python-magic / libmagic

It is a C library and another system package in the image, to identify three
formats whose signatures are four bytes long and have not changed in twenty years.
The dependency costs more than the code it would replace.

## The limit on knowing

`.docx` and `.pptx` are both ZIP archives, so both start with `PK`. The magic
bytes can prove a file *is* a ZIP, and cannot prove which Office format it holds.
That is settled where it can be settled: the extractor opens it, and a `.pptx`
that is really a Word file fails there as a corrupt archive. This is worth being
precise about rather than implying the check is stronger than it is.
"""

from dataclasses import dataclass
from enum import StrEnum

from app.api.errors import FileTooLargeError, UnsupportedFileError


class DocumentKind(StrEnum):
    PDF = "pdf"
    DOCX = "docx"
    PPTX = "pptx"


# Office formats are ZIP archives; a PDF says so in its first four bytes.
_ZIP = b"PK\x03\x04"
_MAGIC: dict[DocumentKind, bytes] = {
    DocumentKind.PDF: b"%PDF",
    DocumentKind.DOCX: _ZIP,
    DocumentKind.PPTX: _ZIP,
}

_BY_EXTENSION = {f".{kind.value}": kind for kind in DocumentKind}

# An empty file passes every signature check by never reaching one, so it is
# refused here rather than becoming a confusing extraction failure later.
MIN_BYTES = len(_ZIP)


@dataclass(frozen=True)
class DetectedFile:
    kind: DocumentKind
    filename: str
    size_bytes: int


def supported_extensions() -> tuple[str, ...]:
    """For error messages and API documentation, so the list is written once."""
    return tuple(sorted(_BY_EXTENSION))


def detect(filename: str, data: bytes, *, max_bytes: int) -> DetectedFile:
    """Identify the file, or refuse it with a reason the caller can act on.

    Raises `UnsupportedFileError` (415) or `FileTooLargeError` (413). Both carry a
    stable `code`, so a client can branch on the kind of problem without reading
    the sentence.
    """
    name = (filename or "").strip()
    if not name:
        raise UnsupportedFileError("The upload had no filename.")

    suffix = name[name.rfind(".") :].lower() if "." in name else ""
    kind = _BY_EXTENSION.get(suffix)
    if kind is None:
        allowed = ", ".join(supported_extensions())
        raise UnsupportedFileError(
            f"{name!r} is not a supported file type. Upload one of: {allowed}."
        )

    if len(data) > max_bytes:
        raise FileTooLargeError(
            f"{name!r} is {_megabytes(len(data))} MB. The limit is "
            f"{_megabytes(max_bytes)} MB."
        )

    if len(data) < MIN_BYTES:
        raise UnsupportedFileError(f"{name!r} is empty.")

    if not data.startswith(_MAGIC[kind]):
        # The name and the contents disagree. Say which, because the usual cause
        # is a genuine mistake — a file saved in the wrong format, or renamed to
        # get past an upload box — and the student can fix it once they know.
        raise UnsupportedFileError(
            f"{name!r} is named like a {kind.value.upper()} file but its contents "
            f"are not one. Re-save it as a real {kind.value.upper()}."
        )

    return DetectedFile(kind=kind, filename=name, size_bytes=len(data))


def _megabytes(size: int) -> str:
    return f"{size / (1024 * 1024):.1f}"
