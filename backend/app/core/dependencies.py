"""Application dependency injection providers."""

from collections.abc import Generator
from typing import Annotated

from fastapi import Depends

from backend.app.config.settings import Settings, get_settings


def provide_settings() -> Settings:
    """Provide application settings to route handlers and services."""

    return get_settings()


SettingsDependency = Annotated[Settings, Depends(provide_settings)]


def provide_request_context() -> Generator[dict[str, str], None, None]:
    """Provide a deliberately minimal request context boundary."""

    context = {"service": "taletrace"}
    yield context


RequestContextDependency = Annotated[dict[str, str], Depends(provide_request_context)]