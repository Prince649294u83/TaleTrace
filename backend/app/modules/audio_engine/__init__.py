"""Reading Audio Engine module boundary.

Coordinates synchronized narration for active reading sessions: pointer,
playback state, delivery profile, and speech provider. It does not own the
reading pointer (the Reading Engine does) and it does not generate content.
"""

from backend.app.modules.audio_engine.audio_profiles import get_profile, list_profiles
from backend.app.modules.audio_engine.interfaces import (
    PlaybackEngineInterface,
    PointerManagerInterface,
    SentenceQueueInterface,
    SpeechProviderInterface,
)
from backend.app.modules.audio_engine.models import (
    AudioProfile,
    EmphasisLevel,
    PauseReason,
    PlaybackState,
    PlaybackStatistics,
    PlaybackStatus,
    ReadingPointer,
    SentenceChunk,
    SpeechRequest,
    SpeechResponse,
    Voice,
)
from backend.app.modules.audio_engine.playback_engine import PlaybackEngine
from backend.app.modules.audio_engine.pointer_manager import PointerManager
from backend.app.modules.audio_engine.sentence_queue import (
    SentenceQueue,
    segment_sentences,
)
from backend.app.modules.audio_engine.session_manager import (
    AudioSessionManager,
    session_manager,
)
from backend.app.modules.audio_engine.speech_provider import (
    AudioSink,
    EdgeSpeechProvider,
    FakeSpeechProvider,
    LocalAudioSink,
    NullAudioSink,
    OfflineSpeechProvider,
    get_provider,
)
from backend.app.modules.audio_engine.state_machine import (
    InvalidTransition,
    PlaybackStateMachine,
)

__all__ = [
    "AudioProfile",
    "AudioSessionManager",
    "AudioSink",
    "EdgeSpeechProvider",
    "EmphasisLevel",
    "FakeSpeechProvider",
    "InvalidTransition",
    "LocalAudioSink",
    "NullAudioSink",
    "OfflineSpeechProvider",
    "PauseReason",
    "PlaybackEngine",
    "PlaybackEngineInterface",
    "PlaybackState",
    "PlaybackStateMachine",
    "PlaybackStatistics",
    "PlaybackStatus",
    "PointerManager",
    "PointerManagerInterface",
    "ReadingPointer",
    "SentenceChunk",
    "SentenceQueue",
    "SentenceQueueInterface",
    "SpeechProviderInterface",
    "SpeechRequest",
    "SpeechResponse",
    "Voice",
    "get_profile",
    "get_provider",
    "list_profiles",
    "segment_sentences",
    "session_manager",
]
