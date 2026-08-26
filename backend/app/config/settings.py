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

    # Relative on purpose: the file belongs next to the checkout that produced
    # it, so a developer with two clones does not silently share one reading
    # history between them.
    database_url: str = "sqlite:///taletrace.db"

    # Vite's dev server. A setting rather than a literal because the port moves
    # when 5173 is taken, and a hardcoded origin fails as a blank dashboard with
    # a CORS error only visible in the browser console.
    cors_origins: tuple[str, ...] = (
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """Return cached runtime settings."""

    return Settings()