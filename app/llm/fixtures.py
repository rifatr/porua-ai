"""Recording and replaying real Gemini responses.

Why this exists at all: the test suite must run with no API key and give the same
answer every time. But hand-written fixtures alone would only ever test the model
we *imagine*. Recording real responses once, then replaying them, tests the
behaviour the model actually has.

Three modes, set by LLM_FIXTURE_MODE:

    live    - call the provider, record nothing (default when running the app)
    record  - call the provider and save the response as a fixture
    replay  - never touch the network; a missing fixture is a test failure

`replay` failing loudly is the important part. If a missing fixture silently fell
through to a live call, a test suite that looks offline would quietly start
costing money and producing different results on every run.

Hand-written fixtures for malformed output live separately, under
tests/fixtures/llm/synthetic/ — the real model will not produce a quiz with
duplicate options on demand, which is exactly why those must be written by hand.
"""

import hashlib
import json
import logging
from pathlib import Path

from app.llm.base import LLMError, LLMRequest, LLMResponse

logger = logging.getLogger(__name__)

FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "llm" / "recorded"


def fixture_key(request: LLMRequest) -> str:
    """A stable id for one request.

    Everything that changes the response goes into the key, so editing a prompt
    produces a new key rather than silently reusing the old recording.
    """
    material = json.dumps(
        {
            "model": request.model,
            "temperature": request.temperature,
            "max_output_tokens": request.max_output_tokens,
            # Constraining the output changes what comes back, so it has to change
            # the key. Without it a recording made before structured output was
            # turned on would be replayed as if it were still valid.
            "response_schema": (
                request.response_schema.__name__ if request.response_schema else None
            ),
            "prompt": request.prompt,
        },
        sort_keys=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


class FixtureMissing(LLMError):
    def __init__(self, key: str) -> None:
        super().__init__(
            "FIXTURE_MISSING",
            f"No recorded response for key {key}. "
            f"Re-run with LLM_FIXTURE_MODE=record and a valid GEMINI_API_KEY.",
        )


def save(request: LLMRequest, response: LLMResponse) -> None:
    FIXTURE_ROOT.mkdir(parents=True, exist_ok=True)
    key = fixture_key(request)
    (FIXTURE_ROOT / f"{key}.json").write_text(
        json.dumps(
            {
                # Stored for a human reading the file later; not used on load.
                "_request": request.as_params() | {"prompt_preview": request.prompt[:400]},
                "text": response.text,
                "finish_reason": response.finish_reason,
                "prompt_tokens": response.prompt_tokens,
                "output_tokens": response.output_tokens,
                "thought_tokens": response.thought_tokens,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    logger.info("recorded llm fixture %s", key)


def load(request: LLMRequest) -> LLMResponse:
    key = fixture_key(request)
    path = FIXTURE_ROOT / f"{key}.json"
    if not path.is_file():
        raise FixtureMissing(key)

    data = json.loads(path.read_text(encoding="utf-8"))
    return LLMResponse(
        text=data["text"],
        finish_reason=data.get("finish_reason", "STOP"),
        prompt_tokens=data.get("prompt_tokens", 0),
        output_tokens=data.get("output_tokens", 0),
        thought_tokens=data.get("thought_tokens", 0),
        raw=data["text"],
    )


class RecordingClient:
    """Wraps a real client and saves everything it returns."""

    def __init__(self, inner) -> None:
        self._inner = inner

    async def generate(self, request: LLMRequest) -> LLMResponse:
        response = await self._inner.generate(request)
        save(request, response)
        return response


class ReplayClient:
    """Serves saved responses. Never touches the network."""

    async def generate(self, request: LLMRequest) -> LLMResponse:
        return load(request)
