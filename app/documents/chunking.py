"""Cutting extracted text into searchable pieces.

A whole document is the wrong unit to search. Ask "what did the handout say about
photosynthesis" and a match on a 40-page PDF tells you the answer is *somewhere*
in it, which is not an answer. Chunks make a hit specific enough to quote.

## The three rules

**Roughly 250 words per chunk.** Small enough that a hit points at a paragraph
rather than a chapter; large enough that a sentence keeps the context that makes
it mean something.

**40 words of overlap.** A chunk boundary that lands mid-explanation would leave
neither side making sense. Overlapping means any short passage appears whole in at
least one chunk. The cost is about 15% more rows, which buys the guarantee that no
sentence is split in every copy of itself.

**Never across a page boundary.** A chunk carries the page number it came from,
and that is what the tutor cites. One chunk spanning pages 3 and 4 could only
claim one of them, so half of what it says would be cited to the wrong place.
Pages are therefore chunked independently, even when that leaves a short one.

## Why words and not tokens

A token count would need the model's tokeniser, which is a second dependency
answering a question we do not actually have. Nothing here is sized against a hard
model limit — the search tool returns a fixed number of chunks, and the budget
that matters is enforced there. Words are close enough, and anyone can count them.
"""

from dataclasses import dataclass

from app.documents.extract import Page

WORDS_PER_CHUNK = 250
OVERLAP_WORDS = 40


@dataclass(frozen=True)
class Chunk:
    chunk_no: int
    page_no: int | None
    text: str


def chunk_pages(pages: list[Page]) -> list[Chunk]:
    """Every page cut into overlapping pieces, numbered across the document.

    `chunk_no` runs 1..n over the whole document rather than restarting on each
    page, so it orders the document on its own and a chunk can be found from its
    number alone.
    """
    chunks: list[Chunk] = []
    for page in pages:
        for text in _split(page.text):
            chunks.append(Chunk(chunk_no=len(chunks) + 1, page_no=page.number, text=text))
    return chunks


def _split(text: str) -> list[str]:
    """One page's text as overlapping runs of words.

    Whitespace is normalised on the way through. The original layout is not worth
    preserving: it is not shown to anyone, and blank lines and stray indentation
    would otherwise count against the word budget.
    """
    words = text.split()
    if not words:
        return []
    if len(words) <= WORDS_PER_CHUNK:
        return [" ".join(words)]

    # Each chunk starts OVERLAP_WORDS before the previous one ended, so `step` is
    # the number of genuinely new words per chunk.
    step = WORDS_PER_CHUNK - OVERLAP_WORDS
    pieces = [
        " ".join(words[start : start + WORDS_PER_CHUNK])
        for start in range(0, len(words), step)
    ]

    # The last window can be entirely overlap — when the words divide evenly by
    # `step`, the final slice repeats the tail of the one before it and adds
    # nothing. Dropping it avoids a duplicate row that would match twice.
    if len(pieces) > 1 and len(words) - (len(pieces) - 1) * step <= OVERLAP_WORDS:
        pieces.pop()

    return pieces
