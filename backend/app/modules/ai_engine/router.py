"""Placeholder router for ai_engine."""
from fastapi import APIRouter
from backend.app.models.responses import ResponseEnvelope
from backend.app.modules.ai_engine.schemas import AiExplainRequest, AiExplainResponse

router = APIRouter(prefix="/ai", tags=["ai_engine"])

@router.post("/explain", response_model=ResponseEnvelope[AiExplainResponse])
def explain_content(_request: AiExplainRequest) -> ResponseEnvelope[AiExplainResponse]:
    return ResponseEnvelope(message="AI explanation placeholder", data=AiExplainResponse(status="pending"))