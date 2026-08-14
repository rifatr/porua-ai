"""Pulling text out of a PDF, a Word file or a PowerPoint.

Every extractor returns the same thing — a list of `Page` — so everything
downstream is written once and does not care which format it came from.

## Why a page number is not always available

A PDF stores pages. A PowerPoint stores slides. A **Word file stores neither**:
`.docx` holds a stream of paragraphs, and page breaks are computed by whatever
renders it, from the font metrics and paper size in use at the time. Open the same
file on another machine and "page 4" can be a different paragraph.

So `page_no` is `None` for Word, and a citation for a Word file names the file
without a page. Inventing a number would be worse than omitting one: the tutor
would quote it to the student, the student would turn to page 4, and it would not
be there. A missing citation is a gap; a confident wrong one is a lie.

## What is not extracted

Every gap here is silent. A file with text in one of these places produces no
error and no warning — it produces a document that quietly does not contain what
the student can see on their screen, and the first sign of trouble is the tutor
saying the material does not cover something it visibly does. That is why the
list is written out rather than left to be discovered.

**Word (`.docx`)** — headers, footers, text boxes, footnotes, endnotes, comments
and embedded chart data. `python-docx` walks the document body, and none of those
live in it. Headers and footers are the one that bites: a cover page's date,
course code or supervisor name is very often in a footer, and none of it reaches
us. Fixing it means reading the package's other XML parts directly.

**PowerPoint (`.pptx`)** — SmartArt (a drawing, not text), chart data, WordArt
and images. Text inside **grouped** shapes *is* read: a group is a shape holding
shapes and has no text of its own, so iterating only the top level silently lost
whole diagrams and their labels. `_flatten` below recurses for that reason.

**PDF** — images, and any formula or figure rendered as one. A two-column layout
can come out interleaved; PyMuPDF reads in layout order, which is usually right
and is not always right.

**All three** — no OCR. A scanned PDF is *rejected*, not accepted as empty, and
so is one whose fonts carry no character map. Accepting either would leave a
document in the room that looks searchable, matches nothing, and gives no clue
why.

A separate limit belongs to search rather than extraction, and is worth knowing
here because it looks like an extraction failure: full-text search matches words,
so it cannot answer "what is the submission date" from a slide reading
`AUGUST 2026`. The date is extracted perfectly and is unfindable by the word
"date". See the README's more-time list.
"""

import logging
import unicodedata
import zipfile
from dataclasses import dataclass
from io import BytesIO

import pymupdf
from docx import Document as DocxDocument
from docx.table import Table as DocxTable
from docx.text.paragraph import Paragraph as DocxParagraph
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

from app.api.errors import UnreadableFileError
from app.documents.detect import DocumentKind

logger = logging.getLogger(__name__)

# Control characters that carry structure and are kept.
_KEEP_CONTROL = frozenset("\n\r\t")

# `Cc` is control characters, `Cs` lone surrogates. Neither can survive a round
# trip into Postgres. `Cf` is **not** here on purpose — see `clean`.
_DROP_CATEGORIES = frozenset({"Cc", "Cs"})

# What counts as evidence of a broken encoding. `Co` is the private-use area:
# valid Unicode that means whatever the embedding font says it means, which is
# exactly the information a file like this is missing.
_UNREADABLE_CATEGORIES = frozenset({"Cc", "Cs", "Co"})

# Above this share of unreadable characters the extraction is not text. Real
# documents score ~0 and a broken one scores far higher, so the threshold sits
# in a wide empty gap rather than on a judgement call.
MAX_CONTROL_RATIO = 0.10


@dataclass(frozen=True)
class Page:
    """Text from one page or slide, or from a whole Word file.

    `number` is 1-based, or None where the format has no such thing.
    """

    number: int | None
    text: str


def extract(kind: DocumentKind, data: bytes) -> list[Page]:
    """Text from the file, one entry per page or slide.

    Raises `UnreadableFileError` for every way a well-formed upload can still be
    unusable. Anything else escaping this function is a bug.
    """
    extractors = {
        DocumentKind.PDF: _extract_pdf,
        DocumentKind.DOCX: _extract_docx,
        DocumentKind.PPTX: _extract_pptx,
    }
    pages = extractors[kind](data)

    raw = "".join(page.text for page in pages)
    if control_ratio(raw) > MAX_CONTROL_RATIO:
        # Not a damaged file — it opens, it has pages, it renders correctly in a
        # PDF viewer. What it lacks is the map from glyph back to character, so
        # what comes out is font indices rather than words. Rejected rather than
        # stored, because storing it puts an unsearchable document in the room
        # that looks exactly like a searchable one.
        #
        # Measured against a real one: a 148-page LaTeX book built with 98 Type 3
        # fonts scored 0.416 across the document, and its only pages under the
        # threshold were blank. `text`, `blocks` and `words` extraction modes all
        # return the same indices, so there is no fallback to try — the file
        # genuinely contains drawings of letters rather than letters.
        raise UnreadableFileError(
            "This file's text cannot be extracted. It looks perfect on screen "
            "because the letters are drawn as shapes, but the file never records "
            "which letters those shapes are, so there is nothing to read out. "
            "Older LaTeX output does this. Reading it would need OCR, which we "
            "do not do — upload a version exported from the original document.",
            code="UNDECODABLE_TEXT",
        )

    pages = [Page(number=page.number, text=clean(page.text)) for page in pages]

    if not any(page.text.strip() for page in pages):
        # Opened cleanly, contained no words. A title-only slide deck or a file
        # holding nothing but images lands here.
        raise UnreadableFileError(
            "The file opened but contained no text to index.",
            code="EMPTY_EXTRACTION",
        )

    return [page for page in pages if page.text.strip()]


def clean(text: str) -> str:
    """Strip characters that must never reach the database.

    Postgres `text` cannot hold a NUL byte — `\\x00` raises
    `CharacterNotInRepertoireError` and turns a bad upload into a 500. Lone
    surrogates cannot be encoded as UTF-8 at all. Both are removed here, at the
    one point every extractor's output passes through, rather than trusted not to
    appear.

    **What is deliberately kept:** category `Cf`, the format characters. That
    includes the zero-width joiner and non-joiner, and Bangla, Hindi and Arabic
    need them to render correctly — `\\u09b0\\u200d\\u09cd` is a different thing
    from `\\u09b0\\u09cd`. A filter written as "strip everything in category C"
    looks tidier and would quietly corrupt the writing this product exists to
    serve. Newlines and tabs are kept for the same reason: they carry structure.
    """
    return "".join(
        character
        for character in text
        if character in _KEEP_CONTROL
        or unicodedata.category(character) not in _DROP_CATEGORIES
    )


def control_ratio(text: str) -> float:
    """How much of this text is characters that cannot be read.

    The signature of a font with no usable encoding. Real text in any language
    scores essentially zero: control characters are not letters in any script,
    and private-use code points mean "ask the font what this is", which is the
    question we could not answer. Measured over the whole document, so one bad
    page among a hundred good ones is cleaned rather than fatal.
    """
    if not text:
        return 0.0
    suspicious = sum(
        1
        for character in text
        if character not in _KEEP_CONTROL
        and unicodedata.category(character) in _UNREADABLE_CATEGORIES
    )
    return suspicious / len(text)


# --- PDF --------------------------------------------------------------------


def _extract_pdf(data: bytes) -> list[Page]:
    try:
        pdf = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:
        # PyMuPDF raises several unrelated exception types for a damaged file.
        # Catching broadly here is deliberate: the caller needs one typed error,
        # and a 500 for a truncated download would be our bug report, not theirs.
        logger.info("pdf would not open: %s", exc)
        raise UnreadableFileError(
            "The PDF is damaged and could not be opened.",
            code="CORRUPT_ARCHIVE",
        ) from exc

    with pdf:
        if pdf.needs_pass:
            raise UnreadableFileError(
                "The PDF is password-protected. Remove the password and upload it "
                "again.",
                code="ENCRYPTED",
            )

        pages = [
            Page(number=index, text=page.get_text("text"))
            for index, page in enumerate(pdf, 1)
        ]
        has_images = any(page.get_images() for page in pdf)

    if not any(page.text.strip() for page in pages) and has_images:
        # Images but no text is the signature of a scan or a photographed
        # handout. Distinguished from EMPTY_EXTRACTION because the fix is
        # different: this one needs OCR, which we do not do, and the student
        # needs to be told that rather than left guessing.
        raise UnreadableFileError(
            "This PDF is a scan — it contains images of text, not text. We cannot "
            "read scanned documents. Upload a version with selectable text.",
            code="NO_TEXT_LAYER",
        )

    return pages


# --- Word -------------------------------------------------------------------


def _extract_docx(data: bytes) -> list[Page]:
    try:
        document = DocxDocument(BytesIO(data))
    except (zipfile.BadZipFile, KeyError, ValueError) as exc:
        # KeyError is what python-docx raises for a ZIP that is missing the parts
        # a Word file must have — which is what a .pptx renamed to .docx looks
        # like, and the case the magic-byte check cannot catch.
        logger.info("docx would not open: %s", exc)
        raise UnreadableFileError(
            "The Word file is damaged, or is not really a Word file.",
            code="CORRUPT_ARCHIVE",
        ) from exc

    blocks: list[str] = []
    # Walking the body rather than `document.paragraphs` keeps tables in the
    # place they appear. Reading paragraphs and then tables would move every
    # table to the end and separate it from the text explaining it.
    for block in _iter_docx_blocks(document):
        if isinstance(block, DocxParagraph):
            text = block.text.strip()
            if text:
                blocks.append(text)
        else:
            blocks.extend(_table_rows(block))

    # One Page, numberless. See the module docstring.
    return [Page(number=None, text="\n\n".join(blocks))]


def _iter_docx_blocks(document: DocxDocument):
    """Paragraphs and tables, in document order.

    python-docx exposes them as two separate lists, so this walks the underlying
    XML body instead and yields whichever comes next.
    """
    body = document.element.body
    for child in body.iterchildren():
        if child.tag.endswith("}p"):
            yield DocxParagraph(child, document)
        elif child.tag.endswith("}tbl"):
            yield DocxTable(child, document)


# --- PowerPoint -------------------------------------------------------------


def _extract_pptx(data: bytes) -> list[Page]:
    try:
        presentation = Presentation(BytesIO(data))
    except (zipfile.BadZipFile, KeyError, ValueError) as exc:
        logger.info("pptx would not open: %s", exc)
        raise UnreadableFileError(
            "The PowerPoint file is damaged, or is not really a PowerPoint file.",
            code="CORRUPT_ARCHIVE",
        ) from exc

    pages: list[Page] = []
    for number, slide in enumerate(presentation.slides, 1):
        parts: list[str] = []

        for shape in _flatten(slide.shapes):
            if shape.has_table:
                parts.extend(_table_rows(shape.table))
            elif shape.has_text_frame:
                text = shape.text_frame.text.strip()
                if text:
                    parts.append(text)

        # Speaker notes are often the most useful study content on a slide: the
        # slide says "Photosynthesis", the notes say what it is.
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                parts.append(f"Speaker notes: {notes}")

        pages.append(Page(number=number, text="\n\n".join(parts)))

    return pages


def _flatten(shapes):
    """Every shape on a slide, including the ones inside groups.

    A group is a shape containing shapes, and it has no text of its own — so
    iterating `slide.shapes` and asking each for its text silently loses
    everything inside one. Decks group constantly: a labelled diagram, a callout
    with its arrow, a figure with its caption. Those are usually the slides worth
    searching, and the loss leaves no trace — no error, no warning, just a
    document that quietly does not contain what the student can see on screen.

    Recursive because groups nest.
    """
    for shape in shapes:
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from _flatten(shape.shapes)
        else:
            yield shape


def _table_rows(table) -> list[str]:
    """A table as one line per row, so a row reads as a unit.

    Cells are joined with " | " because a search hit inside a table should show
    the whole row — a lone cell reading "42" tells the student nothing, while
    "Carbon | 12 | 6" does. Empty rows are dropped rather than left as blank
    lines that would take up a chunk's budget without carrying any words.
    """
    lines: list[str] = []
    for row in table.rows:
        values = [" ".join(cell.text.split()) for cell in row.cells]
        line = " | ".join(value for value in values if value)
        if line:
            lines.append(line)
    return lines
