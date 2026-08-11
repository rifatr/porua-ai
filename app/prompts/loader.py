"""Loading versioned prompt files.

The brief asks for prompts to be "readable and versioned". They live as Markdown
files under `prompts/<name>/v<N>.md`, with YAML front-matter recording the model,
temperature and a changelog.

Three properties this gives us:

1. **Traceability.** Every stored attempt records the prompt name, version and a
   checksum of the exact text. Given any saved answer you can recover the precise
   prompt that produced it — which is only true if a shipped prompt file is never
   edited in place. Change one and you add v2.

2. **Loud failures.** Jinja2 runs with StrictUndefined, so a template that expects
   `education_level` and does not get it raises at render time. The alternative is
   silently rendering an empty string, which produces a quietly worse prompt and
   no error anywhere.

3. **Readability.** A prompt is a document, not a Python string with escaped
   newlines in the middle of a service function.
"""

import hashlib
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from jinja2 import BaseLoader, Environment, StrictUndefined

PROMPTS_ROOT = Path(__file__).resolve().parents[2] / "prompts"

_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", re.DOTALL)

_env = Environment(
    loader=BaseLoader(),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
    autoescape=False,  # prompts are plain text, not HTML
)


class PromptError(RuntimeError):
    """A prompt file is missing or malformed. Always a bug, never user input."""


@dataclass(frozen=True)
class Prompt:
    name: str
    version: int
    model: str
    temperature: float
    body: str
    sha256: str

    def render(self, **variables: Any) -> str:
        """Fill the template. Raises if a variable the template needs is missing."""
        return _env.from_string(self.body).render(**variables).strip()


@lru_cache(maxsize=64)
def load_prompt(name: str, version: int) -> Prompt:
    """Read and parse one prompt file. Cached — files do not change at runtime."""
    path = PROMPTS_ROOT / name / f"v{version}.md"
    if not path.is_file():
        raise PromptError(f"No prompt file at {path}")

    raw = path.read_text(encoding="utf-8")
    match = _FRONT_MATTER.match(raw)
    if not match:
        raise PromptError(f"{path} has no YAML front-matter block")

    meta = yaml.safe_load(match.group(1)) or {}
    body = match.group(2)

    for field in ("name", "version", "model", "temperature"):
        if field not in meta:
            raise PromptError(f"{path} front-matter is missing '{field}'")

    # Guard against a file that was copied to v2 without its metadata updated —
    # otherwise attempts would be recorded against the wrong version.
    if meta["name"] != name or int(meta["version"]) != version:
        raise PromptError(
            f"{path} declares {meta['name']}@v{meta['version']} "
            f"but lives at {name}/v{version}.md"
        )

    return Prompt(
        name=name,
        version=version,
        model=str(meta["model"]),
        temperature=float(meta["temperature"]),
        body=body,
        # Checksum of the whole file, front-matter included: changing the
        # temperature changes the behaviour, so it should change the checksum.
        sha256=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
    )


def latest_version(name: str) -> int:
    """Highest version number on disk for a prompt."""
    directory = PROMPTS_ROOT / name
    if not directory.is_dir():
        raise PromptError(f"No prompt directory at {directory}")
    versions = [
        int(m.group(1))
        for p in directory.glob("v*.md")
        if (m := re.fullmatch(r"v(\d+)\.md", p.name))
    ]
    if not versions:
        raise PromptError(f"No versioned prompt files in {directory}")
    return max(versions)
