from typing import Annotated
from fastapi import APIRouter, Depends
from backend.app.models.responses import ResponseEnvelope
from backend.app.modules.session.schemas import CurrentSessionRequest, SessionEndRequest, SessionResponse, SessionStartRequest

router = APIRouter(prefix="/session", tags=["session"])

@router.post("/start", response_model=ResponseEnvelope[SessionResponse])
def start_session(_request: SessionStartRequest) -> ResponseEnvelope[SessionResponse]:
    return ResponseEnvelope(message="Session start placeholder", data=SessionResponse(status="pending"))

@router.post("/end", response_model=ResponseEnvelope[SessionResponse])
def end_session(_request: SessionEndRequest) -> ResponseEnvelope[SessionResponse]:
    return ResponseEnvelope(message="Session end placeholder", data=SessionResponse(status="pending"))

@router.get("/current", response_model=ResponseEnvelope[SessionResponse])
def current_session(_request: Annotated[CurrentSessionRequest, Depends()]) -> ResponseEnvelope[SessionResponse]:
    return ResponseEnvelope(message="Current session placeholder", data=SessionResponse(status="unavailable"))