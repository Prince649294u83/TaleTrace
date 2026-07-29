"""Placeholder health route for infrastructure verification."""

from typing import Annotated
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.app.core.dependencies import SettingsDependency
from backend.app.models.responses import ResponseEnvelope

router = APIRouter(tags=["health"])

class HealthRequest(BaseModel):
    pass

class HealthResponse(BaseModel):
    status: str
    environment: str

@router.get("/health", response_model=ResponseEnvelope[HealthResponse])
def health_check(_request: Annotated[HealthRequest, Depends()], settings: SettingsDependency) -> ResponseEnvelope[HealthResponse]:
    """Report that the placeholder application process is available."""

    return ResponseEnvelope(data=HealthResponse(status="ok", environment=settings.app_env))