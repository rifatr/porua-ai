"""Uploading, reading and searching study material.

The bad-file cases get as much room here as the good ones, because "handle bad and
unsupported inputs deliberately" is the requirement, and a rejection is only
deliberate if it is the *right* rejection. A scan and a blank page both produce no
text; telling them apart is the whole point, and only a test can hold that apart.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import UnreadableFileError
from app.documents.chunking import OVERLAP_WORDS, WORDS_PER_CHUNK, chunk_pages
from app.documents.detect import DocumentKind
from app.documents.extract import (
    MAX_CONTROL_RATIO,
    Page,
    clean,
    control_ratio,
    extract,
)
from app.models.room import Room
from app.models.student import Student
from app.services import document as document_service
from app.services.document import MAX_DOCUMENTS_PER_ROOM, SearchHit
from app.tools.base import ToolContext
from app.tools.registry import resolve, validate_arguments
from tests import documents as make


async def add_room(db: AsyncSession, student: Student, title: str = "Biology") -> Room:
    room = Room(student_id=student.id, title=title)
    db.add(room)
    await db.flush()
    return room


async def upload(
    client: AsyncClient,
    auth: dict[str, str],
    room: Room,
    filename: str,
    data: bytes,
) -> tuple[int, dict]:
    response = await client.post(
        f"/rooms/{room.id}/documents",
        headers=auth,
        files={"file": (filename, data, "application/octet-stream")},
    )
    return response.status_code, response.json() if response.content else {}


# --- the good cases ---------------------------------------------------------


async def test_a_pdf_is_indexed_with_page_numbers(
    client: AsyncClient, auth: dict[str, str], db: AsyncSession, student: Student
) -> None:
    room = await add_room(db, student)
    status, body = await upload(
        client,
        auth,
        room,
        "handout.pdf",
        make.pdf(["Photosynthesis needs light.", "Respiration releases energy."]),
    )

    assert status == 201, body
    assert body["kind"] == "pdf"
    assert body["page_count"] == 2
    assert body["chunk_count"] == 2


async def test_a_word_file_reports_no_page_count(
    client: AsyncClient, auth: dict[str, str], db: AsyncSession, student: Student
) -> None:
    """Not a bug — a .docx does not contain page numbers. Reporting 1 would be
    inventing a fact about the document."""
    room = await add_room(db, student)
    status, body = await upload(
        client, auth, room, "notes.docx", make.docx(["Cell division has two stages."])
    )

    assert status == 201, body
    assert body["page_count"] is None
    assert body["chunk_count"] == 1


async def test_a_word_table_keeps_its_rows(
    client: AsyncClient, auth: dict[str, str], db: AsyncSession, student: Student
) -> None:
    room = await add_room(db, student)
    await upload(
        client,
        auth,
        room,
        "table.docx",
        make.docx(["Element data:"], table=[["Carbon", "12"], ["Oxygen", "16"]]),
    )

    # A row is searchable as a unit, so a hit on "Carbon" carries its value.
    hits = await run_search(db, room, "Carbon")
    assert "Carbon | 12" in hits[0].text


async def test_slides_are_indexed_with_speaker_notes(
    client: AsyncClient, auth: dict[str, str], db: AsyncSession, student: Student
) -> None:
    """Notes are often the best content on a slide: the slide says the word, the
    notes say what it means."""
    room = await add_room(db, student)
    status, body = await upload(
        client,
        auth,
        room,
        "lecture.pptx",
        make.pptx([("Mitosis", "Four stages")], notes="Prophase, metaphase, anaphase."),
    )
    assert status == 201, body

    # Through the tool, so this also proves what the model actually receives:
    # a finished citation string, not an id it has to render itself.
    result = await run_tool(db, student, room, "metaphase")
    assert result["passages"], "speaker notes were not indexed"
    assert result["passages"][0]["cite"] == "lecture.pptx, page 1"


async def test_text_inside_grouped_shapes_is_not_lost(
    client: AsyncClient, auth: dict[str, str], db: AsyncSession, student: Student
) -> None:
    """A group is a shape holding shapes and has no text of its own, so reading
    only the top-level shapes drops everything inside one — with no error and no
    warning, just a deck that quietly lacks what is on the screen. Decks group
    constantly, and the grouped slides tend to be the ones worth searching."""
    room = await add_room(db, student)
    status, _ = await upload(
        client, auth, room, "diagram.pptx", make.pptx_with_grouped_shapes()
    )
    assert status == 201

    for phrase in ("loose", "grouped", "nested"):
        assert await run_search(db, room, phrase), f"{phrase!r} text was dropped"


async def test_the_same_file_twice_is_not_indexed_twice(
    client: AsyncClient, auth: dict[str, str], db: AsyncSession, student: Student
) -> None:
    """Students re-upload when they are not sure it worked. That is not an error."""
    room = await add_room(db, student)
    data = make.pdf(["Osmosis moves water."])

    first_status, first = await upload(client, auth, room, "bio.pdf", data)
    second_status, second = await upload(client, auth, room, "bio.pdf", data)

    assert (first_status, second_status) == (201, 201)
    assert first["id"] == second["id"]

    listed = await client.get(f"/rooms/{room.id}/documents", headers=auth)
    assert len(listed.json()) == 1


# --- the bad cases ----------------------------------------------------------


async def test_an_unsupported_type_is_rejected(
    client: AsyncClient, auth: dict[str, str], db: AsyncSession, student: Student
) -> None:
    room = await add_room(db, student)
    status, body = await upload(client, auth, room, "notes.txt", b"just text")

    assert status == 415
    assert body["code"] == "UNSUPPORTED_TYPE"


async def test_a_renamed_text_file_is_caught_by_its_contents(
    client: AsyncClient, auth: dict[str, str], db: AsyncSession, student: Student
) -> None:
    """The extension is a string the client chose. The bytes are not."""
    room = await add_room(db, student)
    status, body = await upload(client, auth, room, "fake.pdf", make.not_really_a_pdf())

    assert status == 415
    assert body["code"] == "UNSUPPORTED_TYPE"


async def test_an_oversized_file_is_rejected(
    client: AsyncClient, auth: dict[str, str], db: AsyncSession, student: Student
) -> None:
    room = await add_room(db, student)
    # Valid signature, far past the limit. The size check must not depend on the
    # file being parseable.
    oversized = b"%PDF" + b"\x00" * (21 * 1024 * 1024)
    status, body = await upload(client, auth, room, "big.pdf", oversized)

    assert status == 413
    assert body["code"] == "FILE_TOO_LARGE"


async def test_a_corrupt_pdf_is_rejected(
    client: AsyncClient, auth: dict[str, str], db: AsyncSession, student: Student
) -> None:
    room = await add_room(db, student)
    status, body = await upload(client, auth, room, "broken.pdf", make.corrupt_pdf())

    assert status == 422
    assert body["code"] == "CORRUPT_ARCHIVE"


async def test_a_corrupt_office_file_is_rejected(
    client: AsyncClient, auth: dict[str, str], db: AsyncSession, student: Student
) -> None:
    """Past the magic-byte check — it really does start with PK — and still not a
    Word file. This is the case detection cannot catch, by design."""
    room = await add_room(db, student)
    status, body = await upload(client, auth, room, "broken.docx", make.corrupt_zip())

    assert status == 422
    assert body["code"] == "CORRUPT_ARCHIVE"


async def test_a_powerpoint_named_as_word_is_rejected(
    client: AsyncClient, auth: dict[str, str], db: AsyncSession, student: Student
) -> None:
    room = await add_room(db, student)
    status, body = await upload(
        client, auth, room, "slides.docx", make.pptx([("Title", "Body")])
    )

    assert status == 422
    assert body["code"] == "CORRUPT_ARCHIVE"


async def test_an_encrypted_pdf_is_rejected(
    client: AsyncClient, auth: dict[str, str], db: AsyncSession, student: Student
) -> None:
    room = await add_room(db, student)
    status, body = await upload(client, auth, room, "locked.pdf", make.encrypted_pdf())

    assert status == 422
    assert body["code"] == "ENCRYPTED"


async def test_a_scanned_pdf_is_rejected_rather_than_accepted_empty(
    client: AsyncClient, auth: dict[str, str], db: AsyncSession, student: Student
) -> None:
    """The alternative is worse than an error: a document sitting in the room,
    looking searchable, matching nothing, with no clue why."""
    room = await add_room(db, student)
    status, body = await upload(client, auth, room, "scan.pdf", make.scanned_pdf())

    assert status == 422
    assert body["code"] == "NO_TEXT_LAYER"
    assert "scan" in body["detail"].lower()


async def test_a_pdf_with_no_words_is_rejected(
    client: AsyncClient, auth: dict[str, str], db: AsyncSession, student: Student
) -> None:
    """Distinct from a scan: nothing to OCR either. Different cause, different code."""
    room = await add_room(db, student)
    status, body = await upload(client, auth, room, "blank.pdf", make.empty_pdf())

    assert status == 422
    assert body["code"] == "EMPTY_EXTRACTION"


async def test_nothing_is_saved_when_a_file_is_rejected(
    client: AsyncClient, auth: dict[str, str], db: AsyncSession, student: Student
) -> None:
    room = await add_room(db, student)
    await upload(client, auth, room, "scan.pdf", make.scanned_pdf())

    listed = await client.get(f"/rooms/{room.id}/documents", headers=auth)
    assert listed.json() == []


# --- search -----------------------------------------------------------------


async def run_search(db: AsyncSession, room: Room, query: str) -> list[SearchHit]:
    """The service directly — what the endpoint and the tool both sit on."""
    return await document_service.search(db, room, query)


async def run_tool(db: AsyncSession, student: Student, room: Room, query: str) -> dict:
    """Through the registry, so the tool is reached the way the model reaches it.

    Going through `resolve` and `validate_arguments` rather than calling the class
    means these tests also prove the tool is registered and its arguments fit.
    """
    tool = resolve("search_room_materials")
    assert tool is not None, "search_room_materials is not in the registry"
    args = validate_arguments(tool, {"query": query})
    return await tool.run(args, ToolContext(db=db, student=student, room=room))


async def test_search_finds_the_right_page(
    client: AsyncClient, auth: dict[str, str], db: AsyncSession, student: Student
) -> None:
    room = await add_room(db, student)
    await upload(
        client,
        auth,
        room,
        "bio.pdf",
        make.pdf(
            [
                "Chapter one covers cell structure and the nucleus.",
                "Chapter two covers photosynthesis in the chloroplast.",
            ]
        ),
    )

    hits = await run_search(db, room, "photosynthesis chloroplast")

    assert hits, "expected a match"
    assert hits[0].page_no == 2
    assert hits[0].citation == "bio.pdf, page 2"


async def test_search_cannot_reach_another_rooms_material(
    client: AsyncClient, auth: dict[str, str], db: AsyncSession, student: Student
) -> None:
    """The room is a join, not an argument. There is nothing to set."""
    biology = await add_room(db, student, "Biology")
    algebra = await add_room(db, student, "Algebra")
    await upload(client, auth, biology, "bio.pdf", make.pdf(["Mitochondria make energy."]))

    assert await run_search(db, biology, "mitochondria")
    assert await run_search(db, algebra, "mitochondria") == []


async def test_one_unmatched_word_does_not_empty_the_result(
    client: AsyncClient, auth: dict[str, str], db: AsyncSession, student: Student
) -> None:
    """The bug that made the whole feature look broken in a real room.

    `websearch_to_tsquery` joins terms with AND, so a model writing natural
    language — 'protections prevent arbitrary tool calls' — needs every one of
    those words present. The document said "protections" and "arbitrary" and
    never "prevent", and one ordinary English word turned five good matches into
    none. The model then read the silence as "not covered" and answered from
    general knowledge, confidently and without the handout.
    """
    room = await add_room(db, student)
    await upload(
        client,
        auth,
        room,
        "safety.pdf",
        make.pdf(["Protections against arbitrary tool calls start with a fixed registry."]),
    )

    # Every word present: the strict pass answers.
    assert await run_search(db, room, "protections arbitrary tool calls")
    # One word absent. Must still find it.
    assert await run_search(db, room, "protections prevent arbitrary tool calls")


async def test_the_strict_pass_wins_when_every_word_matches(
    client: AsyncClient, auth: dict[str, str], db: AsyncSession, student: Student
) -> None:
    """Falling back must not cost precision. When all terms are present, the
    chunk containing all of them ranks first rather than being diluted by chunks
    sharing a single common word."""
    room = await add_room(db, student)
    await upload(
        client,
        auth,
        room,
        "bio.pdf",
        make.pdf(
            [
                "The chloroplast is where photosynthesis happens in a plant cell.",
                "The cell wall gives a plant cell its shape and rigidity.",
                "A plant cell also contains a large central vacuole.",
            ]
        ),
    )

    hits = await run_search(db, room, "chloroplast photosynthesis")

    assert hits[0].page_no == 1


async def test_search_survives_query_syntax_a_model_might_produce(
    client: AsyncClient, auth: dict[str, str], db: AsyncSession, student: Student
) -> None:
    """`to_tsquery` would raise on every one of these. `websearch_to_tsquery` does
    not, which is why it is the one we use on model-supplied text."""
    room = await add_room(db, student)
    await upload(client, auth, room, "bio.pdf", make.pdf(["Enzymes speed up reactions."]))

    for query in ["enzymes & &", "enzymes | ", '"unclosed quote', "!!! ???", "and or not"]:
        await run_search(db, room, query)  # must not raise


async def test_deleting_a_document_removes_it_from_search(
    client: AsyncClient, auth: dict[str, str], db: AsyncSession, student: Student
) -> None:
    room = await add_room(db, student)
    _, body = await upload(
        client, auth, room, "bio.pdf", make.pdf(["Ribosomes build proteins."])
    )
    assert await run_search(db, room, "ribosomes")

    response = await client.delete(f"/documents/{body['id']}", headers=auth)
    assert response.status_code == 204

    assert await run_search(db, room, "ribosomes") == []


async def test_another_students_document_cannot_be_deleted(
    client: AsyncClient, auth: dict[str, str], db: AsyncSession, student: Student
) -> None:
    room = await add_room(db, student)
    _, body = await upload(client, auth, room, "bio.pdf", make.pdf(["Private notes."]))

    intruder = Student(display_name="Someone Else", education_level="Grade 9")
    db.add(intruder)
    await db.flush()

    response = await client.delete(
        f"/documents/{body['id']}", headers={"X-Student-Id": str(intruder.id)}
    )
    assert response.status_code == 404


# --- the per-room limit -----------------------------------------------------


async def test_the_oldest_file_is_evicted_rather_than_the_upload_refused(
    client: AsyncClient, auth: dict[str, str], db: AsyncSession, student: Student
) -> None:
    """A student part-way through studying should not hit a wall. The file they
    are reaching for now matters more than one from last term."""
    room = await add_room(db, student)
    for index in range(MAX_DOCUMENTS_PER_ROOM):
        status, _ = await upload(
            client, auth, room, f"note{index}.pdf", make.pdf([f"Note number {index}."])
        )
        assert status == 201

    status, body = await upload(
        client, auth, room, "newest.pdf", make.pdf(["The newest note."])
    )

    assert status == 201, "the upload must not be refused"
    assert body["evicted"] == ["note0.pdf"], "the oldest should go, and be named"

    listed = (await client.get(f"/rooms/{room.id}/documents", headers=auth)).json()
    assert len(listed) == MAX_DOCUMENTS_PER_ROOM
    assert "note0.pdf" not in {d["filename"] for d in listed}
    assert "newest.pdf" in {d["filename"] for d in listed}


async def test_a_full_room_does_not_evict_the_file_just_added(
    client: AsyncClient, auth: dict[str, str], db: AsyncSession, student: Student
) -> None:
    """The obvious way to get this wrong: count the room after inserting, drop
    the oldest, and find the new file was the one dropped in some ordering."""
    room = await add_room(db, student)
    for index in range(MAX_DOCUMENTS_PER_ROOM + 3):
        await upload(client, auth, room, f"n{index}.pdf", make.pdf([f"Note {index}."]))

    hits = await run_search(db, room, f"Note {MAX_DOCUMENTS_PER_ROOM + 2}")
    assert hits, "the most recent upload must still be searchable"


async def test_an_ordinary_upload_evicts_nothing(
    client: AsyncClient, auth: dict[str, str], db: AsyncSession, student: Student
) -> None:
    room = await add_room(db, student)
    _, body = await upload(client, auth, room, "one.pdf", make.pdf(["Only file."]))
    assert body["evicted"] == []


async def test_evicted_material_stops_being_searchable(
    client: AsyncClient, auth: dict[str, str], db: AsyncSession, student: Student
) -> None:
    """Eviction has to reach the chunks too, or the tutor keeps citing a file the
    student can no longer see in the room."""
    room = await add_room(db, student)
    await upload(client, auth, room, "first.pdf", make.pdf(["Xenon is a noble gas."]))
    assert await run_search(db, room, "xenon")

    for index in range(MAX_DOCUMENTS_PER_ROOM):
        await upload(client, auth, room, f"f{index}.pdf", make.pdf([f"Filler {index}."]))

    assert await run_search(db, room, "xenon") == []


# --- what the tutor is told the room contains -------------------------------


async def test_materials_are_described_for_the_prompt(
    client: AsyncClient, auth: dict[str, str], db: AsyncSession, student: Student
) -> None:
    """The line that stops the model guessing the room is empty.

    Without it a student who uploaded a slide deck and asked "summarise the
    thesis" got a general essay about what a thesis is — the model had no way to
    know a specific one was in the room.
    """
    room = await add_room(db, student)
    await upload(client, auth, room, "slides.pptx", make.pptx([("Mitosis", "Stages")]))
    await upload(client, auth, room, "handout.pdf", make.pdf(["One.", "Two."]))
    await upload(client, auth, room, "notes.docx", make.docx(["Some notes."]))

    described = await document_service.describe_materials(db, room)

    # Newest first, sized in the unit the format actually has.
    assert described == [
        "notes.docx",  # Word has no page count to report
        "handout.pdf (2 pages)",
        "slides.pptx (1 slides)",
    ]


async def test_an_empty_room_describes_nothing(
    db: AsyncSession, student: Student
) -> None:
    """So the prompt omits the section entirely rather than announcing a heading
    with nothing under it."""
    room = await add_room(db, student)
    assert await document_service.describe_materials(db, room) == []


# --- text that is not text --------------------------------------------------


def test_a_nul_byte_never_reaches_the_database() -> None:
    """Postgres text cannot hold `\\x00`. Left in, it is a 500 on a bad upload —
    which is the one outcome the bad-file handling exists to prevent."""
    assert "\x00" not in clean("before\x00after")
    assert clean("before\x00after") == "beforeafter"


def test_cleaning_keeps_the_characters_bangla_needs() -> None:
    """The zero-width joiner is category Cf. A filter written as "drop everything
    in category C" would pass every other test here and silently corrupt Bangla,
    Hindi and Arabic — the writing this product exists to serve."""
    joined = "র‍্য"  # র‍্য, needs the ZWJ to render
    assert clean(joined) == joined
    assert clean("café — naïve “quoted”") == "café — naïve “quoted”"


def test_newlines_and_tabs_survive_cleaning() -> None:
    assert clean("one\ntwo\tthree\r\n") == "one\ntwo\tthree\r\n"


def test_real_text_scores_no_control_characters() -> None:
    for text in ("Photosynthesis needs light.", "শিক্ষা", "x = (-b ± √Δ) / 2a", ""):
        assert control_ratio(text) <= MAX_CONTROL_RATIO


def test_glyph_indices_are_recognised_as_undecodable() -> None:
    """What a PDF with no ToUnicode map actually returns: font indices, not
    letters. It renders perfectly in a viewer and cannot be read as text."""
    glyphs = "\x00\x02\x01\x04\x03 \x05\x07\x06 \x08 \x0f\x0e\x11\x10\x13\x12"
    assert control_ratio(glyphs) > MAX_CONTROL_RATIO


def test_an_undecodable_pdf_is_rejected_not_stored(monkeypatch) -> None:
    """The wiring, not just the helper. A PDF whose fonts carry no character map
    is a real file that opens fine, so it can only be caught after extraction —
    and the libraries make one impossible to build here, hence the stand-in.
    """
    glyphs = "\x00\x02\x01\x04\x03 \x05\x07\x06 \x08 \x0f\x0e\x11\x10\x13\x12" * 5
    monkeypatch.setattr(
        "app.documents.extract._extract_pdf",
        lambda data: [Page(number=1, text=glyphs)],
    )

    with pytest.raises(UnreadableFileError) as caught:
        extract(DocumentKind.PDF, b"%PDF-1.7 pretend")

    assert caught.value.code == "UNDECODABLE_TEXT"


def test_one_bad_page_does_not_condemn_a_good_document() -> None:
    """The ratio is measured across the whole document, so a single damaged page
    is cleaned away instead of rejecting ninety-nine good ones."""
    good = "This page is entirely readable text about biology. " * 20
    assert control_ratio(good + "\x00\x01\x02\x03") <= MAX_CONTROL_RATIO


# --- chunking ---------------------------------------------------------------


def test_chunks_never_cross_a_page_boundary() -> None:
    """A chunk carries one page number. One spanning two pages could only claim
    one of them, so half its text would be cited to the wrong place."""
    pages = [Page(number=1, text="alpha " * 400), Page(number=2, text="beta " * 400)]

    chunks = chunk_pages(pages)

    for chunk in chunks:
        words = set(chunk.text.split())
        assert words in ({"alpha"}, {"beta"}), "a chunk mixed two pages"


def test_chunks_overlap_so_no_passage_is_split_in_every_copy() -> None:
    words = [f"w{index}" for index in range(WORDS_PER_CHUNK * 2)]
    chunks = chunk_pages([Page(number=1, text=" ".join(words))])

    assert len(chunks) > 1
    first = chunks[0].text.split()
    second = chunks[1].text.split()
    assert first[-OVERLAP_WORDS:] == second[:OVERLAP_WORDS]


def test_a_short_page_is_one_chunk() -> None:
    chunks = chunk_pages([Page(number=1, text="a short sentence")])
    assert len(chunks) == 1
    assert chunks[0].chunk_no == 1


def test_chunk_numbers_run_across_the_whole_document() -> None:
    """Not restarting per page, so a chunk can be ordered from its number alone."""
    pages = [Page(number=n, text=f"page {n} text") for n in (1, 2, 3)]
    assert [c.chunk_no for c in chunk_pages(pages)] == [1, 2, 3]


def test_the_last_chunk_is_never_pure_overlap() -> None:
    """A trailing window that repeats only what the previous chunk already held
    would match twice and add nothing."""
    step = WORDS_PER_CHUNK - OVERLAP_WORDS
    words = [f"w{index}" for index in range(step * 3)]

    chunks = chunk_pages([Page(number=1, text=" ".join(words))])

    last = set(chunks[-1].text.split())
    previous = set(chunks[-2].text.split())
    assert not last.issubset(previous)
