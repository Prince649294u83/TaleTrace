"""An audio sink that takes as long to "play" as the words would really take.

`NullAudioSink` returns immediately, which is right for tests and wrong for a
rehearsal: a page narrates in microseconds, the reading clock barely moves, and
every pace number on screen is a division by almost zero. Nothing about how
narration and reading speed interact is visible, because nothing takes any time.

This sink sleeps instead. It is handed the sentence it is about to speak and
sleeps for `words / (wpm/60)` seconds, so a 20-word sentence at 150 wpm occupies
eight seconds of wall clock exactly as it would through a real voice. That makes
the two clocks in the dashboard mean something:

    narration pace   the profile's rate, fixed, set by the Audio Engine
    reading pace     what Reading Speed measures the reader actually doing

and it makes the gap between them watchable, which is the entire point of the
rehearsal. A reader keeping up with a 150 wpm narrator and a reader who has
stopped turning pages look identical when every sentence is free.

Deliberately in `demo/`, not in the audio engine. Production sinks play bytes;
this one plays the *passage of time*, which is only ever useful to a simulation.
"""

import asyncio

from backend.app.modules.audio_engine.models import AudioProfile


class SimClock:
    """One clock for the whole rehearsal: simulated seconds, compressed wall time.

    Injected into `ReadingSpeedService` *and* used for every sleep in the demo, so
    the module measures simulated time rather than wall time. Without this the
    compressed run reports a reader doing 1000 wpm — the words are credited at
    full size while the interval they took has been divided by the speed factor.

    `sleep()` is the only thing that moves it. Everything that consumes simulated
    time goes through here, which is what keeps the two honest with each other.
    """

    def __init__(self, *, speed: float = 1.0) -> None:
        self.speed = max(0.01, speed)
        self._now = 0.0

    def __call__(self) -> float:
        """Simulated seconds. Matches `time.monotonic`'s contract."""

        return self._now

    async def sleep(self, seconds: float) -> None:
        self._now += seconds
        await asyncio.sleep(seconds / self.speed)

# What a voice at rate 1.0 speaks in a minute. Edge Neural voices land near
# this, and the exact figure matters less than the ratio between profiles: the
# point on screen is that "disability" narrates slower than "normal", not that
# either matches a particular provider to the word.
BASE_NARRATION_WPM = 150.0

# Below this a sentence is a fragment ("Nobody had gone inside since.") and the
# per-word estimate under-reads the pause a real voice takes around it.
MIN_SENTENCE_SECONDS = 0.4


class PacedAudioSink:
    """Discards the audio, but spends the time speaking it would have cost.

    Time is spent through the shared `SimClock`, not `asyncio.sleep` directly, so
    the seconds this sink consumes are the same seconds Reading Speed measures.
    A rehearsal nobody will sit through is a rehearsal nobody runs, and the
    compression is what makes it watchable — but only if one clock governs both.
    """

    def __init__(self, *, clock: SimClock) -> None:
        self.clock = clock
        self.profile: AudioProfile | None = None
        self._pending_words = 0
        self.spoken_seconds = 0.0

    def expect(self, text: str, profile: AudioProfile | None = None) -> None:
        """Tell the sink what is about to be played.

        The sink is handed bytes, not text, so it cannot count the words itself.
        The engine calls `synthesize` then `play`, so the provider announces the
        sentence here on the way past — which keeps this sink honest rather than
        guessing a duration from the length of a fake audio blob.
        """

        self._pending_words = len(text.split())
        if profile is not None:
            self.profile = profile

    async def play(self, audio: bytes, *, content_type: str = "audio/mpeg") -> None:
        rate = self.profile.rate if self.profile else 1.0
        wpm = BASE_NARRATION_WPM * rate
        seconds = max(MIN_SENTENCE_SECONDS, self._pending_words / (wpm / 60.0))
        self.spoken_seconds += seconds
        self._pending_words = 0
        await self.clock.sleep(seconds)

    async def stop(self) -> None:
        self._pending_words = 0


class AnnouncingProvider:
    """Wraps a provider so the paced sink learns the text and profile.

    Exists because `AudioSink.play` receives only bytes. Rather than widen the
    production sink protocol to carry text it has no use for, the announcement
    happens here, on the synthesis call that already has both.
    """

    provider_name = "paced-fake"

    def __init__(self, inner, sink: PacedAudioSink) -> None:
        self._inner = inner
        self._sink = sink
        self.spoken: list[str] = []

    async def synthesize(self, request):
        self._sink.expect(request.text, request.profile)
        self.spoken.append(request.text)
        response = await self._inner.synthesize(request)
        # Report this wrapper's name so the dashboard shows what actually ran.
        return response.model_copy(update={"provider": self.provider_name})

    async def get_available_voices(self):
        return await self._inner.get_available_voices()
