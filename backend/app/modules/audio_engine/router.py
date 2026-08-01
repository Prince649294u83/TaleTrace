"""Router for audio_engine — thin transport over PlaybackEngine.

Every route does the same four things: resolve the session, translate the
request, call one engine method, and wrap the result. All orchestration lives in
the engine.

`session_id` is a query parameter on every route and defaults to "default", so
a single-reader client can ignore it. Two readers that both omit it share one
engine — pass distinct ids to keep sessions isolated.
"""

from fastapi import APIRouter, Query

from backend.app.models.responses import ResponseEnvelope
from backend.app.modules.audio_engine.audio_profiles import get_profile, list_profiles
from backend.app.modules.audio_engine.models import PlaybackState, PlaybackStatus
from backend.app.modules.audio_engine.schemas import (
    PausePlaybackRequest,
    PlaybackStateResponse,
    ProfileListResponse,
    ProfileRequest,
    RefreshQueueRequest,
    SeekRequest,
    SessionListResponse,
    StartPlaybackRequest,
    VoiceListResponse,
    VoiceRequest,
)
from backend.app.modules.audio_engine.session_manager import session_manager

router = APIRouter(prefix="/audio", tags=["audio_engine"])

SessionId = Query("default", description="Reading session id; sessions are isolated.")


def _state_response(
    session_id: str, message: str, *, applied: bool | None = None
) -> ResponseEnvelope[PlaybackStateResponse]:
    status = session_manager.get(session_id).get_status()
    return ResponseEnvelope(
        success=status.error is None,
        message=message,
        data=PlaybackStateResponse(
            state=status.state,
            session_id=status.session_id,
            pointer=status.pointer,
            profile_name=status.profile_name,
            queue_version=status.queue_version,
            applied=applied,
        ),
        errors=[] if status.error is None else [status.error],
    )


@router.post("/start", response_model=ResponseEnvelope[PlaybackStateResponse])
async def start_playback(
    request: StartPlaybackRequest,
    session_id: str = SessionId,
) -> ResponseEnvelope[PlaybackStateResponse]:
    await session_manager.get(session_id).start(
        pointer=request.pointer,
        text=request.text,
        profile=get_profile(request.profile),
        voice_id=request.voice_id,
    )
    return _state_response(session_id, "Playback started")


@router.post("/pause", response_model=ResponseEnvelope[PlaybackStateResponse])
async def pause_playback(
    request: PausePlaybackRequest | None = None,
    session_id: str = SessionId,
) -> ResponseEnvelope[PlaybackStateResponse]:
    payload = request or PausePlaybackRequest()
    await session_manager.get(session_id).pause(reason=payload.reason)
    return _state_response(session_id, "Playback paused")


@router.post("/resume", response_model=ResponseEnvelope[PlaybackStateResponse])
async def resume_playback(
    session_id: str = SessionId,
) -> ResponseEnvelope[PlaybackStateResponse]:
    await session_manager.get(session_id).resume()
    return _state_response(session_id, "Playback resumed")


@router.post("/stop", response_model=ResponseEnvelope[PlaybackStateResponse])
async def stop_playback(
    session_id: str = SessionId,
) -> ResponseEnvelope[PlaybackStateResponse]:
    await session_manager.get(session_id).stop()
    return _state_response(session_id, "Playback stopped")


@router.post("/seek", response_model=ResponseEnvelope[PlaybackStateResponse])
async def seek_playback(
    request: SeekRequest,
    session_id: str = SessionId,
) -> ResponseEnvelope[PlaybackStateResponse]:
    await session_manager.get(session_id).seek(pointer=request.pointer, text=request.text)
    return _state_response(session_id, "Pointer updated")


@router.post("/refresh-queue", response_model=ResponseEnvelope[PlaybackStateResponse])
async def refresh_queue(
    request: RefreshQueueRequest,
    session_id: str = SessionId,
) -> ResponseEnvelope[PlaybackStateResponse]:
    applied = await session_manager.get(session_id).refresh_queue(
        text=request.text, source_version=request.source_version
    )
    message = (
        "Sentence queue refreshed" if applied else "Refresh ignored (stale source version)"
    )
    return _state_response(session_id, message, applied=applied)


@router.post("/profile", response_model=ResponseEnvelope[PlaybackStateResponse])
async def set_profile(
    request: ProfileRequest,
    session_id: str = SessionId,
) -> ResponseEnvelope[PlaybackStateResponse]:
    session_manager.get(session_id).set_profile(get_profile(request.profile))
    return _state_response(session_id, "Audio profile updated")


@router.post("/voice", response_model=ResponseEnvelope[PlaybackStateResponse])
async def set_voice(
    request: VoiceRequest,
    session_id: str = SessionId,
) -> ResponseEnvelope[PlaybackStateResponse]:
    session_manager.get(session_id).set_voice(request.voice_id)
    return _state_response(session_id, "Voice updated")


@router.get("/status", response_model=ResponseEnvelope[PlaybackStatus])
async def playback_status(session_id: str = SessionId) -> ResponseEnvelope[PlaybackStatus]:
    status = session_manager.get(session_id).get_status()
    return ResponseEnvelope(
        success=status.error is None,
        message="Playback status",
        data=status,
        errors=[] if status.error is None else [status.error],
    )


@router.get("/voices", response_model=ResponseEnvelope[VoiceListResponse])
async def available_voices(session_id: str = SessionId) -> ResponseEnvelope[VoiceListResponse]:
    engine = session_manager.get(session_id)
    voices = await engine.list_voices()
    return ResponseEnvelope(
        message="Available voices",
        data=VoiceListResponse(provider=engine.provider_name, voices=voices),
    )


@router.get("/profiles", response_model=ResponseEnvelope[ProfileListResponse])
async def available_profiles() -> ResponseEnvelope[ProfileListResponse]:
    return ResponseEnvelope(
        message="Available audio profiles",
        data=ProfileListResponse(profiles=list_profiles()),
    )


@router.get("/sessions", response_model=ResponseEnvelope[SessionListResponse])
async def active_sessions() -> ResponseEnvelope[SessionListResponse]:
    """Every live session. Useful for confirming isolation while debugging."""

    statuses = session_manager.statuses()
    return ResponseEnvelope(
        message=f"{len(statuses)} active session(s)",
        data=SessionListResponse(count=len(statuses), sessions=statuses),
    )


@router.delete("/session", response_model=ResponseEnvelope[PlaybackStateResponse])
async def close_session(session_id: str = SessionId) -> ResponseEnvelope[PlaybackStateResponse]:
    """Stop a session and discard its engine.

    Called on Camera OFF / session end. Without it, engines accumulate for the
    life of the process.
    """

    existed = await session_manager.close(session_id)
    # Built by hand rather than via _state_response, which would call
    # session_manager.get() and immediately recreate the engine just discarded.
    return ResponseEnvelope(
        success=True,
        message="Session closed" if existed else "No such session",
        data=PlaybackStateResponse(
            state=PlaybackState.IDLE,
            session_id=session_id,
            applied=existed,
        ),
    )
