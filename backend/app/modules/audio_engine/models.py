"""Reading Audio Engine data contracts.

Pure data only — no logic, no provider calls, no state mutation. Every other
module in the audio engine builds on these shapes, and the Reading Engine
codes against `ReadingPointer` as the shared position contract.
"""

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class PlaybackState(str, Enum):
    """Finite playback lifecycle. Replaces scattered boolean flags.

    IDLE -> READY -> PLAYING -> PAUSED -> PLAYING -> FINISHED
                        |                    ^
                        v                    |
                 WAITING_FOR_POINTER --------+
    """

    IDLE = "idle"
    READY = "ready"
    PLAYING = "playing"
    PAUSED = "paused"
    WAITING_FOR_POINTER = "waiting_for_pointer"
    FINISHED = "finished"


class PauseReason(str, Enum):
    """Why playback was suspended. Meaning Mode also freezes the reading timer."""

    USER = "user"
    MEANING_MODE = "meaning_mode"
    PROVIDER_ERROR = "provider_error"


class EmphasisLevel(str, Enum):
    """How strongly difficult words are stressed."""

    NONE = "none"
    MODERATE = "moderate"
    HIGH = "high"


class ReadingPointer(BaseModel):
    """Structured index into the current reading position.

    Shared across every module: the Gesture Engine updates it, the Reading
    Engine owns it, the Audio and AI Engines consume copies.

    Indices are used rather than matched text because a word like 'portfolio'
    may appear several times on one page; an index is unambiguous.

    `character_offset` is carried but not yet honoured for resume — providers
    must emit word-boundary events before sub-sentence resume is possible.
    Storing it now means the contract does not change when that lands.
    """

    model_config = ConfigDict(frozen=True)

    page_index: int = Field(default=1, ge=1)
    paragraph_index: int = Field(default=0, ge=0)
    sentence_index: int = Field(default=0, ge=0)
    character_offset: int = Field(default=0, ge=0)

    def next_sentence(self) -> "ReadingPointer":
        """Pointer to the following sentence in the same paragraph."""

        return self.model_copy(
            update={"sentence_index": self.sentence_index + 1, "character_offset": 0}
        )

    def at_sentence_start(self) -> "ReadingPointer":
        """Same position with the character offset cleared."""

        return self.model_copy(update={"character_offset": 0})

    def at_paragraph_start(self) -> "ReadingPointer":
        """Same page and paragraph, positioned at its first sentence."""

        return self.model_copy(update={"sentence_index": 0, "character_offset": 0})

    def sentence_order_key(self) -> tuple[int, int, int]:
        """Sortable position, ignoring character offset.

        Lexicographic, so a later paragraph always outranks an earlier one
        regardless of how each paragraph numbers its sentences.
        """

        return (self.page_index, self.paragraph_index, self.sentence_index)

    def same_sentence_as(self, other: "ReadingPointer") -> bool:
        """True when both pointers address the same sentence, ignoring offset."""

        return (
            self.page_index == other.page_index
            and self.paragraph_index == other.paragraph_index
            and self.sentence_index == other.sentence_index
        )


class AudioProfile(BaseModel):
    """Speech delivery settings for one reading mode.

    Content never changes between profiles — only delivery. Providers map
    these to their own parameters (SSML prosody, rate strings, and so on).
    """

    model_config = ConfigDict(frozen=True)

    name: str
    rate: float = Field(default=1.0, ge=0.5, le=2.0)
    pitch_shift: int = Field(default=0, ge=-10, le=10)
    pause_after_sentence_ms: int = Field(default=250, ge=0, le=3000)
    emphasis_level: EmphasisLevel = EmphasisLevel.NONE


class SentenceChunk(BaseModel):
    """One unit of speech queued for playback."""

    model_config = ConfigDict(frozen=True)

    text: str
    pointer: ReadingPointer
    duration_estimate_ms: int | None = Field(default=None, ge=0)


class Voice(BaseModel):
    """A voice offered by the active speech provider."""

    id: str
    name: str
    locale: str | None = None
    gender: str | None = None


class SpeechRequest(BaseModel):
    """What the playback engine hands a provider."""

    text: str
    profile: AudioProfile
    voice_id: str | None = None


class SpeechResponse(BaseModel):
    """Result of one synthesis call.

    `audio` is left out of the API layer; only playback consumes the bytes.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    audio: bytes | None = None
    content_type: str = "audio/mpeg"
    provider: str = "unknown"
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class PlaybackStatistics(BaseModel):
    """Analytics for one playback session. Narration, not the reader.

    Every field counts what the TTS engine did: sentences and words it spoke,
    pages it spoke them from, milliseconds it spent speaking. None of it
    measures reading progress — narration follows the pointer and never moves
    it, so what was spoken and what was covered are different quantities.

    Two clocks, deliberately. `reading_time_ms` is speaking time with paused
    intervals removed, so Meaning Mode does not deflate `average_wpm`;
    `playback_time_ms` is wall time since playback began.

    Neither is `SessionAnalytics.reading_duration_ms`. That is the *reader's*
    reading clock and comes from `ProgressSnapshot`. `reading_time_ms` is the
    tempting field and the wrong one: sourcing Reading Speed from it is the
    defect that made a narrated hour report ten words read, and the guard
    against its return lives in `tests/test_reading_speed.py`.

    A sentence is counted once it finishes uninterrupted. One cut off by a pause
    is requeued and spoken again, so counting at synthesis time would double it.
    """

    sentences_spoken: int = 0
    words_spoken: int = 0
    characters_spoken: int = 0
    pages_read: int = 0
    pause_count: int = 0
    meaning_mode_count: int = 0
    reading_updates: int = 0
    queue_refreshes: int = 0
    stale_updates_rejected: int = 0
    playback_time_ms: int = 0
    reading_time_ms: int = 0
    average_wpm: float = 0.0


class PlaybackStatus(BaseModel):
    """Snapshot of the engine, returned by GET /audio/status."""

    state: PlaybackState = PlaybackState.IDLE
    session_id: str | None = None
    pointer: ReadingPointer | None = None
    current_sentence: str | None = None
    profile_name: str | None = None
    provider: str | None = None
    voice_id: str | None = None
    queued_sentences: int = 0
    queue_version: int = 0
    pause_reason: PauseReason | None = None
    # Retained alongside `statistics.reading_time_ms` for existing callers.
    elapsed_reading_ms: int = 0
    statistics: PlaybackStatistics = Field(default_factory=PlaybackStatistics)
    error: str | None = None


class SceneDecision(BaseModel):
    """The result of a scene-mood evaluation for one paragraph.

    ``audio_tag`` is the scene identity — changes in ``emotion`` or
    ``intensity`` within the same tag adjust volume without restarting the
    track.  A different ``audio_tag`` triggers a crossfade.
    """

    model_config = ConfigDict(frozen=True)

    scene_mood: str = "neutral_narration"
    emotion: str = "neutral"
    intensity: float = 0.3
    audio_tag: str = "sfx_soft_ambient.mp3"

