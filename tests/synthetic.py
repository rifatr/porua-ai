"""Reading the hand-written broken responses.

Kept in files rather than as strings inside tests for two reasons. A response with
literal line breaks inside a JSON string cannot be written as a Python literal
without escaping the very thing under test. And a directory of files is browsable
by someone who wants to know what failures this project handles, which a
collection of triple-quoted strings buried in test functions is not.
"""

from pathlib import Path

SYNTHETIC_DIR = Path(__file__).parent / "fixtures" / "llm" / "synthetic"


def synthetic(name: str) -> str:
    """One hand-written response, exactly as the model would have sent it."""
    path = SYNTHETIC_DIR / name
    if not path.is_file():
        available = ", ".join(sorted(p.name for p in SYNTHETIC_DIR.iterdir()))
        raise FileNotFoundError(f"No synthetic fixture {name!r}. Available: {available}")
    # Only the trailing newline that every text file ends with is removed. The
    # content is otherwise untouched, including any leading whitespace, because
    # leading whitespace before a code fence is part of what the parser must cope
    # with.
    return path.read_text(encoding="utf-8").rstrip("\n")
