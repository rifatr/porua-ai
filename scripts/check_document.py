#!/usr/bin/env python
"""Will this file upload cleanly? Answers without starting the API.

    docker compose exec -T api python scripts/check_document.py /tmp/book.pdf

Runs the exact detection and extraction the upload endpoint runs, then prints
what would happen. Useful before a demo: a PDF that turns out to be a scan or a
LaTeX file with Type 3 fonts is better found now than on a screen share.
"""

import sys
from pathlib import Path

from app.api.errors import AppError
from app.config import get_settings
from app.documents.chunking import chunk_pages
from app.documents.detect import detect
from app.documents.extract import control_ratio, extract


def main(path: Path) -> int:
    data = path.read_bytes()
    print(f"{path.name}  ({len(data) / 1024:.0f} KB)")

    try:
        detected = detect(path.name, data, max_bytes=get_settings().max_upload_bytes)
        pages = extract(detected.kind, data)
    except AppError as exc:
        print(f"\n  REJECTED  {exc.status_code} {exc.code}")
        print(f"  {exc.detail}")
        return 1

    chunks = chunk_pages(pages)
    numbered = [page.number for page in pages if page.number]
    text = " ".join(page.text for page in pages)

    print("\n  ACCEPTED")
    print(f"  kind          {detected.kind.value}")
    print(f"  pages         {max(numbered) if numbered else 'n/a (Word has none)'}")
    print(f"  chunks        {len(chunks)}")
    print(f"  words         {len(text.split()):,}")
    print(f"  unreadable    {control_ratio(text):.1%}")
    print(f"\n  first chunk:  {chunks[0].text[:200]!r}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(f"usage: {sys.argv[0]} <file>")
    sys.exit(main(Path(sys.argv[1])))
