"""Health routes for the OCR verification service."""

from fastapi import APIRouter

router = APIRouter(tags=["health"])

@router.get("/", response_model=str)
def service_root() -> str:
    return "TaleTrace OCR Service Running"


@router.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "healthy"}
