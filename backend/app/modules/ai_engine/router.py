"""Router for ai_engine — wired to real Groq-backed AI capabilities."""

from fastapi import APIRouter

from backend.app.models.responses import ResponseEnvelope
from backend.app.modules.ai_engine.engines import (
    ExplanationEngine,
    ImageDecision,
    NovelMode,
    SummaryGenerator,
)
from backend.app.modules.ai_engine.models import (
    AiCapabilityResponse,
    AiExplainRequest,
    AiExplainResponse,
    AiInput,
    AiSessionSummaryRequest,
)

router = APIRouter(prefix="/ai", tags=["ai_engine"])

_explanation_engine = ExplanationEngine()
_novel_mode = NovelMode()
_summary_generator = SummaryGenerator()
_image_decision = ImageDecision()


@router.post("/explain", response_model=ResponseEnvelope[AiExplainResponse])
def explain_content(request: AiExplainRequest) -> ResponseEnvelope[AiExplainResponse]:
    result = _explanation_engine.explain(request)
    return ResponseEnvelope(
        success=result.status == "ok",
        message="AI explanation generated",
        data=AiExplainResponse(
            status=result.status,
            capability=result.capability,
            message=result.message,
            data=result.data,
        ),
        errors=[] if result.status == "ok" else [result.data.get("error", "")],
    )


@router.post("/novel-mode", response_model=ResponseEnvelope[AiCapabilityResponse])
def novel_mode_content(request: AiInput) -> ResponseEnvelope[AiCapabilityResponse]:
    result = _novel_mode.create(request)
    return ResponseEnvelope(
        success=result.status == "ok",
        message="Novel Mode classification generated",
        data=result,
        errors=[] if result.status == "ok" else [result.data.get("error", "")],
    )


@router.post("/session-summary", response_model=ResponseEnvelope[AiCapabilityResponse])
def session_summary_content(
    request: AiSessionSummaryRequest,
) -> ResponseEnvelope[AiCapabilityResponse]:
    result = _summary_generator.summarize(request)
    return ResponseEnvelope(
        success=result.status == "ok",
        message="Session summary generated",
        data=result,
        errors=[] if result.status == "ok" else [result.data.get("error", "")],
    )


@router.post("/image-decision", response_model=ResponseEnvelope[AiCapabilityResponse])
def image_decision_content(request: AiInput) -> ResponseEnvelope[AiCapabilityResponse]:
    result = _image_decision.decide(request)
    return ResponseEnvelope(
        success=result.status == "ok",
        message="Image decision generated",
        data=result,
        errors=[] if result.status == "ok" else [result.data.get("error", "")],
    )
