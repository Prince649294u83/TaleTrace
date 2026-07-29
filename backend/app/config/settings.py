"""Environment-backed application settings."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """TaleTrace runtime configuration."""

    app_name: str = "TaleTrace API"
    app_env: str = "development"
    debug: bool = False
    log_level: str = "INFO"
    host: str = "127.0.0.1"
    port: int = 8000
    google_cloud_vision_api_key: str | None = None
    latest_image_path: Path = Path("latest.jpg")
    processed_image_path: Path = Path("output/processed.jpg")
    ocr_output_path: Path = Path("output/output.txt")
    max_image_bytes: int = 10 * 1024 * 1024

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