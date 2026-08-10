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
    turn_deadline_seconds: int = 45
    tool_timeout_seconds: int = 8
    max_tool_calls_per_turn: int = 6
    max_agent_iterations: int = 5
    max_repair_attempts: int = 2

    # --- Paging ---
    default_page_size: int = 20
    max_page_size: int = 100


@lru_cache
def get_settings() -> Settings:
    """Cached so the .env file is read once, not on every request."""
    return Settings()
