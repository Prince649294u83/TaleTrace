"""Placeholder router for ocr."""
from fastapi import APIRouter
from backend.app.models.responses import ResponseEnvelope
from backend.app.modules.ocr.schemas import OcrProcessRequest, OcrProcessResponse

router = APIRouter(prefix="/ocr", tags=["ocr"])

@router.post("/process", response_model=ResponseEnvelope[OcrProcessResponse])
def process_ocr(_request: OcrProcessRequest) -> ResponseEnvelope[OcrProcessResponse]:
    return ResponseEnvelope(message="OCR processing placeholder", data=OcrProcessResponse(status="pending"))