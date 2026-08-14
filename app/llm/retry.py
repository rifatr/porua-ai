"""Which provider errors are worth sending again, and how long to wait first.

Lives beside the errors themselves rather than in either caller, because both the
tutor loop and the skill loop need exactly this decision and a second copy of it
would drift. The tutor had it first; skills failed on the first error until this
was pulled out, which meant a rate limit that cost the tutor half a second cost a
quiz the whole run.

Nothing here writes a row. The caller records the failed attempt *before* asking
whether to retry, so a retried call still leaves its own `turn_attempts` row —
a retry hidden inside a wrapper writes no row, and then the inspection endpoint
under-reports what the turn actually cost.
"""

import asyncio
import logging
import random

from app.config import Settings
from app.llm.base import EmptyResponse, ProviderUnavailable

logger = logging.getLogger(__name__)

# Worth retrying because it may come out differently next time. A rate limit
# passes; an empty response usually means gemini-2.5-flash spent its whole
# output budget thinking, and how much it thinks varies run to run at
# temperature > 0.
#
# `ResponseBlocked` is deliberately absent. Safety decisions are deterministic,
# so the same prompt is refused again and a retry buys only a slower failure.
RETRIABLE = (ProviderUnavailable, EmptyResponse)


async def backoff(settings: Settings, retry_number: int) -> None:
    """Wait before re-sending, longer each time, with jitter.

    Growing the gap gives an overloaded provider room to recover instead of being
    hit again immediately. The random part matters when several requests fail at
    once: without it they would all wake at the same instant and rate-limit each
    other again, which is the thundering-herd problem.
    """
    base = settings.retry_base_delay_seconds
    if base <= 0:  # tests set this to 0 so the suite does not sleep
        return
    delay = base * (2**retry_number) * (0.5 + random.random())
    logger.info("backing off %.2fs before retry %d", delay, retry_number)
    await asyncio.sleep(delay)
