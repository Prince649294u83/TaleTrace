"""Placeholder router for image_receiver."""
from fastapi import APIRouter
from backend.app.models.responses import ResponseEnvelope
from backend.app.modules.image_receiver.schemas import UploadFrameRequest, UploadFrameResponse

router = APIRouter(tags=["image_receiver"])

@router.post("/upload_frame", response_model=ResponseEnvelope[UploadFrameResponse])
def upload_frame(_request: UploadFrameRequest) -> ResponseEnvelope[UploadFrameResponse]:
    return ResponseEnvelope(message="Frame upload placeholder", data=UploadFrameResponse(status="pending"))