"""Placeholder health route for infrastructure verification."""

from fastapi import APIRouter

from backend.app.core.dependencies import SettingsDependency
from backend.app.models.responses import ResponseEnvelope

router = APIRouter(tags=["health"])


@router.get("/health", response_model=ResponseEnvelope[dict[str, str]])
def health_check(settings: SettingsDependency) -> ResponseEnvelope[dict[str, str]]:
    """Report that the placeholder application process is available."""

    return ResponseEnvelope(data={"status": "ok", "environment": settings.app_env})