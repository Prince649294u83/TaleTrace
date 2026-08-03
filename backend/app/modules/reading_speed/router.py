"""Router for reading_speed — thin transport over ReadingSpeedService.

Every route resolves the session, calls one service method, and wraps the result.
No arithmetic here: prediction lives in `predictor`, inference in `analytics`.

`session_id` is a path segment on the session routes rather than a defaulted
query parameter. The audio engine defaults it to "default" so a single-reader
client can ignore it; here an unknown session is an error instead, because a
prediction silently attributed to an invented session would report a plausible
pace for a session that never started.

Nothing on this surface can move a reader. There is no seek, no pause of
playback, no content write — the POSTs report events that already happened
elsewhere.
"""

import logging

from fastapi import APIRouter, HTTPException, status

from backend.app.models.responses import ResponseEnvelope
from backend.app.modules.reading_speed.calibration import CalibrationError
from backend.app.modules.reading_speed.schemas import (
    BaselineResponse,
    CalibratePassageRequest,
    ContentUpdateRequest,
    ContentUpdateResponse,
    FinishSessionRequest,
    ManualBaselineRequest,
    MeaningModeRequest,
    PointerUpdateRequest,
    PredictionResponse,
    ProgressResponse,
    SessionListResponse,
    SessionSummaryResponse,
    StartSessionRequest,
)
from backend.app.modules.reading_speed.service import reading_speed_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/reading-speed", tags=["reading_speed"])


def _require_session(session_id: str) -> None:
    """404 for a session that was never started.

    Creating one on demand would be worse than failing: its clock would start at
    this moment, and the resulting pace would look entirely reasonable while
    describing nothing.
    """

    if not reading_speed_service.has(session_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no tracked reading session '{session_id}'",
        )


def _progress_response(session_id: str, message: str) -> ResponseEnvelope[ProgressResponse]:
    return ResponseEnvelope(
        success=True,
        message=message,
        data=ProgressResponse(
            progress=reading_speed_service.tracker(session_id).snapshot(),
            observed_wpm=reading_speed_service.observed_wpm(session_id),
        ),
    )


# ------------------------------------------------------------------ calibration


@router.post("/calibrate", response_model=ResponseEnvelope[BaselineResponse])
async def calibrate(request: CalibratePassageRequest) -> ResponseEnvelope[BaselineResponse]:
    """Measure a baseline from a timed passage. Runs once per reader."""

    try:
        baseline = reading_speed_service.calibrate(
            request.reader_id,
            word_count=request.word_count,
            elapsed_ms=request.elapsed_ms,
        )
    except CalibrationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    return ResponseEnvelope(
        success=True,
        message=f"baseline measured at {baseline.baseline_wpm:.0f} wpm",
        data=BaselineResponse(baseline=baseline, is_evidence=baseline.is_evidence),
    )


@router.post("/baseline", response_model=ResponseEnvelope[BaselineResponse])
async def set_manual_baseline(
    request: ManualBaselineRequest,
) -> ResponseEnvelope[BaselineResponse]:
    """Record a reader-supplied pace."""

    try:
        baseline = reading_speed_service.set_manual_baseline(
            request.reader_id, baseline_wpm=request.baseline_wpm
        )
    except CalibrationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    return ResponseEnvelope(
        success=True,
        message=f"baseline set to {baseline.baseline_wpm:.0f} wpm (self-reported)",
        data=BaselineResponse(baseline=baseline, is_evidence=baseline.is_evidence),
    )


@router.get("/baseline/{reader_id}", response_model=ResponseEnvelope[BaselineResponse])
async def get_baseline(reader_id: str) -> ResponseEnvelope[BaselineResponse]:
    """This reader's baseline, defaulting rather than 404ing.

    A reader who has never calibrated still gets a baseline, marked DEFAULT so the
    client can tell the number is an assumption and offer calibration.
    """

    baseline = reading_speed_service.baseline_for(reader_id)
    return ResponseEnvelope(
        success=True,
        message=f"baseline {baseline.baseline_wpm:.0f} wpm ({baseline.method.value})",
        data=BaselineResponse(baseline=baseline, is_evidence=baseline.is_evidence),
    )


# --------------------------------------------------------------------- sessions


@router.post("/sessions/{session_id}/start", response_model=ResponseEnvelope[ProgressResponse])
async def start_session(
    session_id: str, request: StartSessionRequest
) -> ResponseEnvelope[ProgressResponse]:
    """SESSION_STARTED. Starting an already-tracked session restarts its clocks."""

    reading_speed_service.start_session(
        session_id=session_id,
        reader_id=request.reader_id,
        pointer=request.pointer,
        content=request.content,
    )
    return _progress_response(session_id, "session started")


@router.post("/sessions/{session_id}/pause", response_model=ResponseEnvelope[ProgressResponse])
async def pause_session(session_id: str) -> ResponseEnvelope[ProgressResponse]:
    """SESSION_PAUSED. Stops the reading clock; the wall clock runs on."""

    _require_session(session_id)
    reading_speed_service.pause(session_id)
    return _progress_response(session_id, "reading clock paused")


@router.post("/sessions/{session_id}/resume", response_model=ResponseEnvelope[ProgressResponse])
async def resume_session(session_id: str) -> ResponseEnvelope[ProgressResponse]:
    """SESSION_RESUMED."""

    _require_session(session_id)
    reading_speed_service.resume(session_id)
    return _progress_response(session_id, "reading clock resumed")


@router.post("/sessions/{session_id}/pointer", response_model=ResponseEnvelope[ProgressResponse])
async def update_pointer(
    session_id: str, request: PointerUpdateRequest
) -> ResponseEnvelope[ProgressResponse]:
    """READING_POINTER_UPDATED. A page change is inferred from the pointer.

    This stores a *copy*. The Reading Engine still owns the pointer, and posting
    here does not move the reader.
    """

    _require_session(session_id)
    reading_speed_service.update_pointer(session_id, request.pointer, corrected=request.corrected)
    return _progress_response(session_id, "pointer copy updated")


@router.post(
    "/sessions/{session_id}/meaning-mode", response_model=ResponseEnvelope[ProgressResponse]
)
async def set_meaning_mode(
    session_id: str, request: MeaningModeRequest
) -> ResponseEnvelope[ProgressResponse]:
    """MEANING_MODE_ON / MEANING_MODE_OFF. Freezes the reading clock while on."""

    _require_session(session_id)
    reading_speed_service.meaning_mode(session_id, active=request.active)
    return _progress_response(
        session_id, "meaning mode on" if request.active else "meaning mode off"
    )


@router.post("/sessions/{session_id}/lookup", response_model=ResponseEnvelope[ProgressResponse])
async def lookup_completed(session_id: str) -> ResponseEnvelope[ProgressResponse]:
    """LOOKUP_COMPLETED. Friction evidence for the current page."""

    _require_session(session_id)
    reading_speed_service.lookup_completed(session_id)
    return _progress_response(session_id, "lookup recorded")


@router.post(
    "/sessions/{session_id}/content", response_model=ResponseEnvelope[ContentUpdateResponse]
)
async def update_content(
    session_id: str, request: ContentUpdateRequest
) -> ResponseEnvelope[ContentUpdateResponse]:
    """Adopt a Merge Memory map. A stale version is rejected, not an error."""

    _require_session(session_id)
    applied = reading_speed_service.update_content(session_id, request.content)
    return ResponseEnvelope(
        success=True,
        message="content map applied" if applied else "content map rejected as stale",
        data=ContentUpdateResponse(
            applied=applied,
            source_version=reading_speed_service.tracker(session_id).content.source_version,
        ),
    )


# ------------------------------------------------------------------- prediction


@router.get(
    "/sessions/{session_id}/prediction", response_model=ResponseEnvelope[PredictionResponse]
)
async def get_prediction(session_id: str) -> ResponseEnvelope[PredictionResponse]:
    """Where the reader would be at their baseline pace.

    Computed on read, so this is safe to poll as often as a UI wants to redraw —
    there is no cached value to go stale and no loop to fall behind.
    """

    _require_session(session_id)
    prediction = reading_speed_service.predict(session_id)
    return ResponseEnvelope(
        success=True,
        message=(
            f"{abs(prediction.deviation_words)} words "
            f"{'ahead of' if prediction.is_ahead else 'behind'} the baseline"
            if prediction.deviation_words
            else "on pace with the baseline"
        ),
        data=PredictionResponse(
            prediction=prediction,
            observed_wpm=reading_speed_service.observed_wpm(session_id),
        ),
    )


@router.get("/sessions/{session_id}/progress", response_model=ResponseEnvelope[ProgressResponse])
async def get_progress(session_id: str) -> ResponseEnvelope[ProgressResponse]:
    """Observed progress, with no prediction attached."""

    _require_session(session_id)
    return _progress_response(session_id, "current progress")


@router.post(
    "/sessions/{session_id}/finish", response_model=ResponseEnvelope[SessionSummaryResponse]
)
async def finish_session(
    session_id: str, request: FinishSessionRequest
) -> ResponseEnvelope[SessionSummaryResponse]:
    """SESSION_FINISHED. Stops the clocks and returns the summary.

    The baseline moves only when `apply_suggested_baseline` is set, so fetching a
    summary twice cannot move it twice.
    """

    _require_session(session_id)
    analytics = reading_speed_service.finish_session(session_id, playback=request.playback)

    applied = None
    if request.apply_suggested_baseline:
        applied = reading_speed_service.apply_suggested_baseline(analytics)

    return ResponseEnvelope(
        success=True,
        message=(
            f"{analytics.words_read} words at {analytics.session_wpm:.0f} wpm "
            f"across {analytics.pages_read} page(s)"
        ),
        data=SessionSummaryResponse(analytics=analytics, baseline_applied=applied),
    )


@router.delete("/sessions/{session_id}", response_model=ResponseEnvelope[SessionListResponse])
async def close_session(session_id: str) -> ResponseEnvelope[SessionListResponse]:
    """Discard a session's tracker, whether or not it was finished."""

    closed = reading_speed_service.close(session_id)
    ids = reading_speed_service.session_ids()
    return ResponseEnvelope(
        success=True,
        message="session discarded" if closed else "no such session",
        data=SessionListResponse(session_ids=ids, count=len(ids)),
    )


@router.get("/sessions", response_model=ResponseEnvelope[SessionListResponse])
async def list_sessions() -> ResponseEnvelope[SessionListResponse]:
    """Every tracked session. Exists to make cross-session leakage visible."""

    ids = reading_speed_service.session_ids()
    return ResponseEnvelope(
        success=True,
        message=f"{len(ids)} tracked session(s)",
        data=SessionListResponse(session_ids=ids, count=len(ids)),
    )
