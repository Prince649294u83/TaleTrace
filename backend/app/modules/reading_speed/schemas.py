"""Request and response schemas for the Reading Speed API.

Transport shapes only. The domain models in `models.py` are returned directly
wherever they already fit — wrapping `ReadingPrediction` in a near-identical
response class would be two shapes to keep in step and no gain.
"""

from pydantic import BaseModel, Field

from backend.app.modules.audio_engine.models import PlaybackStatistics, ReadingPointer
from backend.app.modules.reading_speed.models import (
    MAX_PLAUSIBLE_WPM,
    MIN_PLAUSIBLE_WPM,
    ContentMap,
    ProgressSnapshot,
    ReadingBaseline,
    ReadingPrediction,
    SessionAnalytics,
)


class CalibratePassageRequest(BaseModel):
    """Measure a baseline from a timed calibration passage.

    The reader reads a known passage and the client times it: 180 words in
    60000ms is 180 wpm. Implausible combinations are rejected rather than
    clamped, since a wrong baseline recorded silently is permanent.
    """

    reader_id: str
    word_count: int = Field(ge=1, description="Words in the passage the reader read.")
    elapsed_ms: int = Field(ge=1, description="Reading time, excluding any pauses.")


class ManualBaselineRequest(BaseModel):
    """Record a reader-supplied pace.

    Kept distinct from a measured baseline: a self-reported speed is a claim, and
    readers routinely overestimate.
    """

    reader_id: str
    baseline_wpm: float = Field(ge=MIN_PLAUSIBLE_WPM, le=MAX_PLAUSIBLE_WPM)


class StartSessionRequest(BaseModel):
    """SESSION_STARTED. Begins tracking; starts both clocks.

    `content` is Merge Memory's description of the text — counts and pointers, no
    strings. Omit it and predictions fall back to the tracker's own word tally
    until a map arrives.
    """

    reader_id: str
    pointer: ReadingPointer = Field(default_factory=ReadingPointer)
    content: ContentMap | None = None


class PointerUpdateRequest(BaseModel):
    """READING_POINTER_UPDATED. A copy of the position its owner just moved.

    Set `corrected` when the reader dragged the pointer back because it was in
    the wrong place. That is friction evidence; ordinary forward movement is not.
    """

    pointer: ReadingPointer
    corrected: bool = False


class MeaningModeRequest(BaseModel):
    """MEANING_MODE_ON / MEANING_MODE_OFF."""

    active: bool


class ContentUpdateRequest(BaseModel):
    """A refreshed Merge Memory map.

    `source_version` must not go backwards: OCR updates travel over HTTP and an
    older frame applied after a newer one would shrink the word counts underneath
    a prediction already in flight.
    """

    content: ContentMap


class FinishSessionRequest(BaseModel):
    """SESSION_FINISHED. Stops the clocks and returns the summary.

    Supply `playback` when TTS was on: the audio engine counted spoken words while
    speaking them, which is more accurate than anything inferred from pointer
    movement, and `tts_assisted` records that this session was paced by narration.
    """

    playback: PlaybackStatistics | None = None
    apply_suggested_baseline: bool = Field(
        default=False,
        description=(
            "Adopt the suggested baseline if the session carried enough evidence. "
            "Off by default so reading a summary never changes the reader's profile."
        ),
    )


class BaselineResponse(BaseModel):
    """A baseline plus whether it can be treated as evidence.

    `is_evidence` is surfaced explicitly because it decides what a client may
    claim: deviation measured against a default baseline is not a finding.
    """

    baseline: ReadingBaseline
    is_evidence: bool


class ProgressResponse(BaseModel):
    """Observed progress, with the session pace computed alongside it.

    `observed_wpm` is deliberately not a field on `ProgressSnapshot`: a snapshot
    carrying its own pace would be a second source of truth competing with the
    baseline. It is derived here, at the transport edge, where nothing can
    mistake it for stored state.
    """

    progress: ProgressSnapshot
    observed_wpm: float


class PredictionResponse(BaseModel):
    """A prediction and the baseline it was measured against.

    Nothing in here is an instruction. No consumer should change playback,
    pointer, or content because of it.
    """

    prediction: ReadingPrediction
    observed_wpm: float


class SessionSummaryResponse(BaseModel):
    """End-of-session analysis.

    `baseline_applied` is present when the caller asked to adopt the suggested
    baseline, so the response shows whether it actually moved — an unchanged
    baseline and a rejected one are otherwise indistinguishable.
    """

    analytics: SessionAnalytics
    baseline_applied: ReadingBaseline | None = None


class ContentUpdateResponse(BaseModel):
    """Whether a Merge Memory map was adopted.

    `applied=False` means the map was stale and rejected. That is a normal
    outcome, not an error.
    """

    applied: bool
    source_version: int


class SessionListResponse(BaseModel):
    """Live tracked sessions, for debugging and isolation checks."""

    session_ids: list[str]
    count: int
