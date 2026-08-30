"""Speech providers and audio sinks.

The playback engine talks to `SpeechProviderInterface` and never learns which
provider is active, so swapping Edge for the offline fallback changes nothing
upstream.

Both third-party providers are imported lazily. `edge-tts` and `pyttsx3` are
optional: the module imports cleanly without them, and a provider only fails
when you actually try to speak through it.
"""

import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Protocol

from backend.app.modules.audio_engine.models import (
    AudioProfile,
    SpeechRequest,
    SpeechResponse,
    Voice,
)

logger = logging.getLogger(__name__)

DEFAULT_EDGE_VOICE = os.getenv("EDGE_TTS_VOICE", "en-US-AriaNeural")


def _rate_to_edge_percent(rate: float) -> str:
    """Edge expects a relative rate string such as '-25%' or '+10%'."""

    percent = int(round((rate - 1.0) * 100))
    return f"{percent:+d}%"


def _pitch_to_edge_hz(semitones: int) -> str:
    """Edge expects a pitch offset in Hz; ~20 Hz per semitone is close enough."""

    return f"{semitones * 20:+d}Hz"


class AudioSink(Protocol):
    """Where synthesized audio goes.

    Separated from the provider so tests can run silently, and so the ESP32
    speaker path can replace local playback later without touching providers.
    """

    async def play(self, audio: bytes, *, content_type: str = "audio/mpeg") -> None:
        """Play audio to completion. Must return when playback finishes."""
        ...

    async def stop(self) -> None:
        """Interrupt playback immediately (Meaning Mode pause)."""
        ...


class NullAudioSink:
    """Discards audio. Used by tests and by headless runs."""

    async def play(self, audio: bytes, *, content_type: str = "audio/mpeg") -> None:
        return None

    async def stop(self) -> None:
        return None


# Plays a file and exits when it ends. WPF's MediaPlayer is used rather than
# SoundPlayer (WAV only) or the WMPlayer COM object (needs a message pump):
# it decodes MP3, reports the real duration, and stops when the process is
# killed — which is what Meaning Mode needs from a sink.
_POWERSHELL_PLAY = (
    "Add-Type -AssemblyName presentationCore;"
    "$p = New-Object System.Windows.Media.MediaPlayer;"
    "$p.Open([uri]'__TALETRACE_AUDIO_PATH__');"
    # Open() is asynchronous; NaturalDuration is not populated until the media
    # is loaded, so poll briefly instead of trusting the first read.
    "$n = 0; while (-not $p.NaturalDuration.HasTimeSpan -and $n -lt 50)"
    " { Start-Sleep -Milliseconds 40; $n++ };"
    "$p.Play();"
    "if ($p.NaturalDuration.HasTimeSpan)"
    " { Start-Sleep -Milliseconds ([int]$p.NaturalDuration.TimeSpan.TotalMilliseconds + 250) }"
    " else { Start-Sleep -Seconds 10 };"
    "$p.Close()"
)
# The script is full of PowerShell braces, so substitute the path by replacement
# rather than str.format, which would read them as field markers.
_POWERSHELL_PATH_TOKEN = "__TALETRACE_AUDIO_PATH__"


class LocalAudioSink:
    """Plays audio through whichever player is available on this machine.

    Keeps a handle on the child process so `stop()` can cut playback off
    mid-sentence, which is what Meaning Mode needs.
    """

    _CANDIDATES: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("ffplay", ("-nodisp", "-autoexit", "-loglevel", "quiet")),
        ("mpv", ("--no-video", "--really-quiet")),
        ("afplay", ()),
        ("aplay", ("-q",)),
    )

    # Windows ships no command-line audio player, so without this the sink
    # discards every sentence and playback appears instantaneous — which hides
    # exactly the timing behaviour the simulator exists to exercise.
    _WINDOWS_FALLBACK = ("powershell", "powershell.exe", "pwsh")

    def __init__(self) -> None:
        self._process: subprocess.Popen | None = None

    def _find_player(self) -> tuple[str, tuple[str, ...]] | None:
        for name, args in self._CANDIDATES:
            if shutil.which(name):
                return name, args
        return None

    def _find_powershell(self) -> str | None:
        for name in self._WINDOWS_FALLBACK:
            if shutil.which(name):
                return name
        return None

    def _build_command(self, path: Path) -> list[str] | None:
        player = self._find_player()
        if player is not None:
            name, args = player
            return [name, *args, str(path)]

        shell = self._find_powershell()
        if shell is not None:
            # Single-quoted inside PowerShell: double any quote in the path so a
            # directory name cannot terminate the string early.
            escaped = str(path).replace("'", "''")
            return [
                shell,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                _POWERSHELL_PLAY.replace(_POWERSHELL_PATH_TOKEN, escaped),
            ]
        return None

    async def play(self, audio: bytes, *, content_type: str = "audio/mpeg") -> None:
        if not audio:
            return

        suffix = ".wav" if "wav" in content_type else ".mp3"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
            handle.write(audio)
            path = Path(handle.name)

        try:
            command = self._build_command(path)
            if command is None:
                logger.warning(
                    "No audio player found (tried ffplay, mpv, afplay, aplay, "
                    "powershell); audio discarded"
                )
                return

            self._process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            await asyncio.to_thread(self._process.wait)
        finally:
            self._process = None
            path.unlink(missing_ok=True)

    async def stop(self) -> None:
        process = self._process
        if process is not None and process.poll() is None:
            process.terminate()


class EdgeSpeechProvider:
    """Microsoft Edge Read Aloud voices via the `edge-tts` package.

    Free and high quality, but unofficial: it impersonates the Edge browser
    against a Bing endpoint. It has broken before when Microsoft rotated the
    `Sec-MS-GEC` anti-abuse token, it is sensitive to system-clock skew, and
    datacenter IP ranges are blocked outright. Good for a laptop demo, not
    something to bet a deployment on — hence `OfflineSpeechProvider`.
    """

    provider_name = "edge"

    def __init__(self, voice_id: str | None = None) -> None:
        self._voice_id = voice_id or DEFAULT_EDGE_VOICE

    async def synthesize(self, request: SpeechRequest) -> SpeechResponse:
        try:
            import edge_tts
        except ImportError:
            return SpeechResponse(
                provider=self.provider_name,
                error="edge-tts is not installed (pip install edge-tts)",
            )

        profile = request.profile
        try:
            communicate = edge_tts.Communicate(
                text=request.text,
                voice=request.voice_id or self._voice_id,
                rate=_rate_to_edge_percent(profile.rate),
                pitch=_pitch_to_edge_hz(profile.pitch_shift),
            )
            audio = bytearray()
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio.extend(chunk["data"])
        except Exception as e:  # noqa: BLE001 — reported so callers can fall back
            logger.warning("Edge TTS synthesis failed: %s", e)
            return SpeechResponse(provider=self.provider_name, error=str(e))

        return SpeechResponse(
            audio=bytes(audio),
            content_type="audio/mpeg",
            provider=self.provider_name,
        )

    async def get_available_voices(self) -> list[Voice]:
        try:
            import edge_tts
        except ImportError:
            return []

        try:
            entries = await edge_tts.list_voices()
        except Exception as e:  # noqa: BLE001 — an empty list is a fine answer here
            logger.warning("Edge TTS voice listing failed: %s", e)
            return []

        return [
            Voice(
                id=entry.get("ShortName", ""),
                name=entry.get("FriendlyName", entry.get("ShortName", "")),
                locale=entry.get("Locale"),
                gender=entry.get("Gender"),
            )
            for entry in entries
        ]


class OfflineSpeechProvider:
    """Local fallback built on `pyttsx3`.

    Robotic, but it needs no network, no credentials, and no third-party
    endpoint. `pyttsx3` speaks through the system audio device itself, so it
    returns no bytes and bypasses the sink entirely.
    """

    provider_name = "offline"

    def __init__(self, voice_id: str | None = None) -> None:
        self._voice_id = voice_id

    def _render_wav_blocking(
        self, text: str, profile: AudioProfile, voice_id: str | None
    ) -> bytes:
        import tempfile
        import pyttsx3

        engine = pyttsx3.init()
        engine.setProperty("rate", int(200 * profile.rate))
        chosen = voice_id or self._voice_id
        if chosen:
            engine.setProperty("voice", chosen)

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
            temp_path = Path(handle.name)

        try:
            engine.save_to_file(text, str(temp_path))
            engine.runAndWait()
            engine.stop()
            with open(temp_path, "rb") as f:
                return f.read()
        finally:
            temp_path.unlink(missing_ok=True)

    async def synthesize(self, request: SpeechRequest) -> SpeechResponse:
        try:
            import pyttsx3  # noqa: F401 — presence check before threading out
        except ImportError:
            return SpeechResponse(
                provider=self.provider_name,
                error="pyttsx3 is not installed (pip install pyttsx3)",
            )

        try:
            wav_bytes = await asyncio.to_thread(
                self._render_wav_blocking, request.text, request.profile, request.voice_id
            )
        except Exception as e:  # noqa: BLE001 — surfaced to the caller
            logger.warning("Offline synthesis failed: %s", e)
            return SpeechResponse(provider=self.provider_name, error=str(e))

        return SpeechResponse(
            audio=wav_bytes,
            content_type="audio/wav",
            provider=self.provider_name,
        )

    async def get_available_voices(self) -> list[Voice]:
        try:
            import pyttsx3
        except ImportError:
            return []

        def _list() -> list[Voice]:
            engine = pyttsx3.init()
            voices = engine.getProperty("voices")
            engine.stop()
            return [
                Voice(
                    id=voice.id,
                    name=getattr(voice, "name", voice.id),
                    locale=(getattr(voice, "languages", None) or [None])[0]
                    if isinstance(getattr(voice, "languages", None), list)
                    else None,
                    gender=getattr(voice, "gender", None),
                )
                for voice in voices
            ]

        try:
            return await asyncio.to_thread(_list)
        except Exception as e:  # noqa: BLE001
            logger.warning("Offline voice listing failed: %s", e)
            return []


class FakeSpeechProvider:
    """Deterministic provider for tests. Never touches network or audio hardware."""

    provider_name = "fake"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.spoken: list[str] = []

    async def synthesize(self, request: SpeechRequest) -> SpeechResponse:
        if self.fail:
            return SpeechResponse(provider=self.provider_name, error="synthesis failed")
        self.spoken.append(request.text)
        return SpeechResponse(audio=b"fake-audio", provider=self.provider_name)

    async def get_available_voices(self) -> list[Voice]:
        return [Voice(id="fake-voice", name="Fake Voice", locale="en-US")]


_PROVIDERS = {
    "edge": EdgeSpeechProvider,
    "offline": OfflineSpeechProvider,
    "fake": FakeSpeechProvider,
}


def get_provider(name: str | None = None, *, voice_id: str | None = None):
    """Build a provider by name, defaulting to `AUDIO_PROVIDER` or Edge."""

    key = (name or os.getenv("AUDIO_PROVIDER") or "edge").strip().lower()
    provider_class = _PROVIDERS.get(key, EdgeSpeechProvider)
    if provider_class is FakeSpeechProvider:
        return provider_class()
    return provider_class(voice_id=voice_id)


def list_provider_names() -> list[str]:
    return list(_PROVIDERS)
