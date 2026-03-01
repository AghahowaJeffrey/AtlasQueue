"""Application settings — loaded once at import time from environment variables."""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Database ────────────────────────────────────────────────────────────
    database_url: str = (
        "postgresql+asyncpg://atlasqueue:atlasqueue@localhost:5432/atlasqueue"
    )

    # ── Redis ───────────────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"

    # ── Queue ───────────────────────────────────────────────────────────────
    job_queue_key: str = "atlasqueue:jobs"
    dlq_key: str = "atlasqueue:dlq"

    # ── Worker ──────────────────────────────────────────────────────────────
    worker_lease_seconds: int = 30
    worker_poll_timeout: int = 5  # seconds, used for BLPOP blocking timeout

    # ── Retry policy (matches PRD §9) ───────────────────────────────────────
    retry_base_delay_seconds: int = 5
    retry_jitter_max_seconds: int = 2

    # ── Logging ─────────────────────────────────────────────────────────────
    log_level: str = "INFO"
    service_name: str = "atlasqueue"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached singleton settings instance."""
    return Settings()
