"""Router for ai_engine — wired to real Groq-backed AI capabilities."""

from fastapi import APIRouter
from backend.app.models.responses import ResponseEnvelope
from backend.app.modules.ai_engine.schemas import AiExplainRequest, AiExplainResponse
from backend.app.modules.ai_engine.models import AiInput, AiPlaceholderResponse, AiSessionSummaryRequest
from backend.app.modules.ai_engine.engines import ExplanationEngine, NovelMode, SummaryGenerator, ImageDecision

router = APIRouter(prefix="/ai", tags=["ai_engine"])

_explanation_engine = ExplanationEngine()
_novel_mode = NovelMode()
_summary_generator = SummaryGenerator()
_image_decision = ImageDecision()


@router.post("/explain", response_model=ResponseEnvelope[AiExplainResponse])
def explain_content(request: AiExplainRequest) -> ResponseEnvelope[AiExplainResponse]:
    result = _explanation_engine.explain(request)
    return ResponseEnvelope(
        message="AI explanation generated",
        data=AiExplainResponse(
            status=result.status,
            capability=result.capability,
            message=result.message,
        ),
    )


@router.post("/novel-mode", response_model=ResponseEnvelope[AiPlaceholderResponse])
def novel_mode_content(request: AiInput) -> ResponseEnvelope[AiPlaceholderResponse]:
    result = _novel_mode.create(request)
    return ResponseEnvelope(
        message="Novel Mode classification generated",
        data=result,
    )


@router.post("/session-summary", response_model=ResponseEnvelope[AiPlaceholderResponse])
def session_summary_content(request: AiSessionSummaryRequest) -> ResponseEnvelope[AiPlaceholderResponse]:
    result = _summary_generator.summarize(request)
    return ResponseEnvelope(
        message="Session summary generated",
        data=result,
    )


@router.post("/image-decision", response_model=ResponseEnvelope[AiPlaceholderResponse])
def image_decision_content(request: AiInput) -> ResponseEnvelope[AiPlaceholderResponse]:
    result = _image_decision.decide(request)
    return ResponseEnvelope(
        message="Image decision generated",
        data=result,
    )