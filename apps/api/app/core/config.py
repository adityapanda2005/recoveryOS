"""
Centralized application configuration.

All configuration is loaded from environment variables (.env in dev).
Never hardcode secrets. This module is the single source of truth for
config so it can be swapped for real secret management in production.
"""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database
    database_url: str = "postgresql+psycopg2://recoveros:recoveros_dev_pw@localhost:5432/recoveros"

    # AI Provider
    ai_provider: str = "mock"  # "mock" | "anthropic"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-6"

    # App
    app_env: str = "development"
    log_level: str = "INFO"
    api_secret_key: str = "change_me_in_production"

    # Deterministic policy defaults (AI cannot override these)
    max_retry_attempts: int = 3
    min_retry_cooldown_seconds: int = 1800
    max_communication_attempts: int = 4
    confidence_threshold: float = 0.65
    max_incentive_percent: int = 10


@lru_cache
def get_settings() -> Settings:
    return Settings()
