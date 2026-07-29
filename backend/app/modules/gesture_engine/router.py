"""Placeholder router for gesture_engine."""
from fastapi import APIRouter
from backend.app.models.responses import ResponseEnvelope
from backend.app.modules.gesture_engine.schemas import GestureSelectRequest, GestureSelectResponse

router = APIRouter(prefix="/gesture", tags=["gesture_engine"])

@router.post("/select", response_model=ResponseEnvelope[GestureSelectResponse])
def select_gesture(_request: GestureSelectRequest) -> ResponseEnvelope[GestureSelectResponse]:
    return ResponseEnvelope(message="Gesture selection placeholder", data=GestureSelectResponse(status="pending"))