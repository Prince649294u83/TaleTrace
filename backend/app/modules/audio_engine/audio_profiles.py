"""Audio delivery profiles, one per reading mode.

Content never changes between profiles — only delivery. Adding a reading mode
means adding a profile here, not touching the playback engine.
"""

from backend.app.modules.audio_engine.models import AudioProfile, EmphasisLevel

NORMAL = AudioProfile(
    name="normal",
    rate=1.0,
    pitch_shift=0,
    pause_after_sentence_ms=250,
    emphasis_level=EmphasisLevel.NONE,
)

ADAPTIVE = AudioProfile(
    name="adaptive",
    rate=0.85,
    pitch_shift=0,
    pause_after_sentence_ms=350,
    emphasis_level=EmphasisLevel.MODERATE,
)

DISABILITY = AudioProfile(
    name="disability",
    rate=0.7,
    pitch_shift=0,
    pause_after_sentence_ms=500,
    emphasis_level=EmphasisLevel.HIGH,
)

STUDY = AudioProfile(
    name="study",
    rate=0.9,
    pitch_shift=0,
    pause_after_sentence_ms=400,
    emphasis_level=EmphasisLevel.MODERATE,
)

NOVEL = AudioProfile(
    name="novel",
    rate=0.95,
    pitch_shift=0,
    pause_after_sentence_ms=300,
    emphasis_level=EmphasisLevel.NONE,
)

EXAM = AudioProfile(
    name="exam",
    rate=1.1,
    pitch_shift=0,
    pause_after_sentence_ms=200,
    emphasis_level=EmphasisLevel.NONE,
)

DEFAULT_PROFILE_NAME = "normal"

_PROFILES: dict[str, AudioProfile] = {
    profile.name: profile
    for profile in (NORMAL, ADAPTIVE, DISABILITY, STUDY, NOVEL, EXAM)
}

# AI Engine reading modes that have no dedicated profile fall back to a
# sensible neighbour, so a mode string never fails to resolve.
_MODE_ALIASES: dict[str, str] = {
    "standard": "normal",
    "dyslexic": "disability",
    "dyslexia": "disability",
}


def list_profiles() -> list[AudioProfile]:
    """All available profiles, for GET /audio/profiles."""

    return list(_PROFILES.values())


def get_profile(name: str | None) -> AudioProfile:
    """Resolve a profile or reading-mode name, falling back to Normal.

    Accepts the AI Engine's `ReadingMode` values so callers can pass a reading
    mode straight through without translating it first.
    """

    if not name:
        return _PROFILES[DEFAULT_PROFILE_NAME]

    key = name.strip().lower()
    key = _MODE_ALIASES.get(key, key)
    return _PROFILES.get(key, _PROFILES[DEFAULT_PROFILE_NAME])
