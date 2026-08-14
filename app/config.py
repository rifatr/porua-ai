"""Application settings, loaded from environment variables (and .env locally).

Every value has a type. If DATABASE_URL is missing the app refuses to start,
which is better than failing later on the first request.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Database ---
    database_url: str

    # --- Gemini ---
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"

    # --- App ---
    app_env: str = "local"
    log_level: str = "INFO"
    llm_fixture_mode: str = "live"

    # --- Limits (PLAN.md section 7) ---
    max_upload_bytes: int = 20 * 1024 * 1024
    # The student is waiting synchronously, so this is the outer bound on the
    # whole turn — every other budget must fit inside it.
    turn_deadline_seconds: int = 45
    # A ceiling on one call to the provider. Without it we inherit whatever the
    # SDK defaults to, which means a hung request has no bound we chose — and the
    # turn deadline below cannot save us, because it is checked between calls and
    # a call that never returns never reaches the check.
    llm_request_timeout_seconds: int = 30
    # A skill makes up to three sequential model calls, so it needs more headroom
    # than a conversational turn. A separate number rather than a shared one
    # because the thing being bounded is genuinely different: a student pressing
    # "quiz me" is waiting for a document, not a reply.
    skill_deadline_seconds: int = 90
    tool_timeout_seconds: int = 8
    # Individual tool runs allowed per turn.
    max_tool_calls_per_turn: int = 6
    # Rounds of "call the model, it asks for tools, run them, call again". When
    # this runs out the tools are simply not offered on the next call, so the
    # model has to answer with what it already has rather than the turn failing.
    max_agent_iterations: int = 5

    # --- Reliability (PLAN.md section 9) ---
    # Two independent budgets. A network retry re-sends the *same* prompt after a
    # provider error; a repair sends a *different* prompt because the response was
    # unusable. Keeping them apart matters: three 429s and three bad answers are
    # different problems, and collapsing them into one counter would hide that.
    max_network_retries: int = 2
    max_repair_attempts: int = 2
    retry_base_delay_seconds: float = 0.5
    # Backstop over every budget, so no combination of them can run away. The
    # worst legal case is 1 first call + 5 tool rounds + 2 retries + 2 repairs =
    # 10, so this is never reached in practice; if a turn does reach it, that is a
    # bug worth seeing in the data rather than an unbounded loop.
    max_attempts_per_turn: int = 12

    # --- Paging ---
    default_page_size: int = 20
    max_page_size: int = 100


@lru_cache
def get_settings() -> Settings:
    """Cached so the .env file is read once, not on every request."""
    return Settings()
