"""FastAPI application entry point."""

from fastapi import FastAPI

from backend.app.api.router import api_router
from backend.app.config.settings import get_settings
from backend.app.core.environment import load_environment
from backend.app.core.logging_config import configure_logging

load_environment()
settings = get_settings()
configure_logging(settings.log_level)

app = FastAPI(title=settings.app_name, debug=settings.debug)
app.include_router(api_router)