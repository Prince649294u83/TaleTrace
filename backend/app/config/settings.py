"""Environment-backed application settings."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """TaleTrace runtime configuration."""

    app_name: str = "TaleTrace API"
    app_env: str = "development"
    debug: bool = False
    log_level: str = "INFO"
    host: str = "127.0.0.1"
    port: int = 8000

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    """Return cached runtime settings."""

    return Settings()