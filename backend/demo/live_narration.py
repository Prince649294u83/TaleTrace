"""A narrated, real-time rehearsal of one reading session.

The other demos check behaviour and print a verdict. This one is built to be
*watched*: narration takes as long as speech really takes, the reader moves at a
pace you choose, and every screen says what just happened and why the numbers
moved. It answers the four questions you cannot answer from a checklist.

    how is the voice played        one sentence at a time, from a queue the
                                   Audio Engine owns; the paced sink spends the
                                   real seconds the words would cost
    what is reading speed doing    watching, only. It publishes a prediction and
                                   never touches the queue, the pointer, or TTS
    how many plays are left        queued sentences now, plus the estimate of
                                   how long the rest of the book will take
    what changes what              a pause freezes the reading clock but not the
                                   wall clock; Meaning Mode stops the voice
                                   mid-sentence; a lost camera freezes the
                                   pointer while time keeps running

Run it:

    python -m backend.demo.live_narration              # 4x, ~1 minute
    python -m backend.demo.live_narration --speed 1     # real time
    python -m backend.demo.live_narration --reader 90    # a slow reader

The reader's pace and the narrator's are set independently on purpose. That gap
is the thing Reading Speed exists to measure, and holding them equal would hide
every interesting number on screen.
"""

import argparse
import asyncio

from backend.app.modules.audio_engine.audio_profiles import get_profile
from backend.app.modules.audio_engine.playback_engine import PlaybackEngine
from backend.app.modules.audio_engine.speech_provider import FakeSpeechProvider
from backend.app.modules.reading_speed.service import ReadingSpeedService
from backend.demo import console
from backend.demo.fake_merge_memory import FakeMergeMemory
from backend.demo.fake_session_engine import SimulatedReadingEngine
from backend.demo.paced_sink import (
    BASE_NARRATION_WPM,
    AnnouncingProvider,
    PacedAudioSink,
    SimClock,
)

READER_ID = "live-reader"
SESSION_ID = "sim-live-narration"
BOOK_TITLE = "The Lighthouse"

CALIBRATION_WORDS = 180
CALIBRATION_MS = 60_000


def _bar(value: float, limit: float, width: int = 18) -> str:
    """A crude meter. Two of these side by side show a gap a number hides."""

    if limit <= 0:
        return " " * width
    filled = max(0, min(width, round(width * value / limit)))
    return "#" * filled + "." * (width - filled)


class LiveNarration:
    """Drives one session and narrates it as it happens."""

    def __init__(self, *, speed_factor: float, reader_wpm: float, profile: str) -> None:
        self.speed_factor = speed_factor
        self.reader_wpm = reader_wpm
        self.profile_name = profile

        self.memory = FakeMergeMemory.from_book()
        # Merge Memory starts settled. OCR refinement is a different demo, and a
        # first frame that misreads half the page would put the narration and the
        # word counts out of step for reasons that have nothing to do with pace.
        for _ in range(3):
            self.memory.refine()

        # One clock for the whole rehearsal. Reading Speed measures it, the sink
        # spends it, and every scripted wait moves it — so a compressed run
        # reports the same numbers a real-time run would.
        self.clock = SimClock(speed=speed_factor)

        self.speed = ReadingSpeedService(clock=self.clock)
        self.speed.calibrate(
            READER_ID, word_count=CALIBRATION_WORDS, elapsed_ms=CALIBRATION_MS
        )

        self.sink = PacedAudioSink(clock=self.clock)
        self.provider = AnnouncingProvider(FakeSpeechProvider(), self.sink)
        # The same clock the sink sleeps on and Reading Speed measures. Left on
        # `time.monotonic` the engine times a 10x-compressed run against real
        # seconds, so its narration statistics — sentences and words spoken, the
        # pace this rehearsal exists to show — are all out by the speed factor.
        self.tts = PlaybackEngine(
            provider=self.provider,
            sink=self.sink,
            session_id=SESSION_ID,
            clock=self.clock,
        )
        self.tts.set_profile(get_profile(profile))

        self.reading = SimulatedReadingEngine(
            session_id=SESSION_ID,
            reader_id=READER_ID,
            memory=self.memory,
            speed=self.speed,
            tts=self.tts,
        )
        self.narration_wpm = BASE_NARRATION_WPM * get_profile(profile).rate

    # ------------------------------------------------------------------ output

    def note(self, title: str, body: str = "") -> None:
        """A plain-language caption for what just happened."""

        print()
        print(console.safe(f"  {title}"))
        if body:
            for line in body.split("\n"):
                print(console.safe(f"    {line}"))

    def frame(self, caption: str) -> None:
        """One snapshot of every number that matters, with a caption."""

        sid = self.reading.session_id
        snap = self.speed.tracker(sid).snapshot()
        pred = self.speed.predict(sid)
        base = self.speed.baseline_for(READER_ID)
        status = self.reading.audio_status()
        state = self.reading.state
        content = self.reading.content

        total_words = sum(content.page_word_counts.values())
        done = snap.words_confirmed
        left = max(0, total_words - done)

        print()
        console.rule("-")
        print(console.safe(f"  {caption}"))
        console.rule("-")

        page = snap.pointer.page_index
        chapter = self.memory.chapter(page)
        print(
            console.safe(
                f"  WHERE     page {page}/{self.memory.page_count}"
                f"  para {snap.pointer.paragraph_index}"
                f"  sentence {snap.pointer.sentence_index}"
                f"   [{chapter}]"
            )
        )

        # --- the voice -------------------------------------------------------
        if status is not None and state.tts_enabled:
            speaking = (status.current_sentence or "").strip()
            if len(speaking) > 46:
                speaking = speaking[:43] + "..."
            reason = f" ({status.pause_reason.value})" if status.pause_reason else ""
            print(
                console.safe(
                    f"  VOICE     {status.state.value}{reason}"
                    f"   {self.profile_name} @ {self.narration_wpm:.0f} wpm"
                )
            )
            print(
                console.safe(
                    f"            queued {status.queued_sentences} sentence(s)"
                    f"   spoken {status.statistics.sentences_spoken}"
                    f"   queue v{status.queue_version}"
                )
            )
            if speaking:
                print(console.safe(f'            now: "{speaking}"'))
        else:
            print(console.safe("  VOICE     off - silent reading"))

        # --- the two paces ---------------------------------------------------
        observed = self.speed.observed_wpm(sid)
        limit = max(self.narration_wpm, observed, base.baseline_wpm) * 1.25
        print(
            console.safe(
                f"  NARRATOR  {_bar(self.narration_wpm, limit)}"
                f"  {self.narration_wpm:>5.0f} wpm  (fixed by the profile)"
            )
        )
        print(
            console.safe(
                f"  READER    {_bar(observed, limit)}"
                f"  {observed:>5.0f} wpm  (measured, baseline "
                f"{base.baseline_wpm:.0f})"
            )
        )

        # --- what reading speed concludes ------------------------------------
        drift = pred.deviation_words
        verdict = "on pace"
        if pred.is_behind:
            verdict = f"behind by {abs(drift)} words"
        elif drift > 0:
            verdict = f"ahead by {drift} words"
        print(
            console.safe(
                f"  PREDICTION expected word {pred.expected_word_offset}"
                f"   actual {pred.actual_word_offset}   -> {verdict}"
            )
        )

        # --- how much is left ------------------------------------------------
        remaining_at_reader = (left / observed * 60) if observed > 0 else 0.0
        remaining_at_voice = left / self.narration_wpm * 60
        print(
            console.safe(
                f"  REMAINING {left} of {total_words} words"
                f"   {self.memory.page_count - page} page(s) after this one"
            )
        )
        print(
            console.safe(
                f"            narrator needs {remaining_at_voice / 60:.1f} min"
                + (
                    f"   reader is tracking to {remaining_at_reader / 60:.1f} min"
                    if observed > 0
                    else ""
                )
            )
        )

        # --- the clocks ------------------------------------------------------
        frozen = "" if snap.is_clock_running else "   << FROZEN"
        print(
            console.safe(
                f"  CLOCKS    reading {snap.elapsed_reading_ms / 1000:>5.1f}s"
                f"   wall {snap.elapsed_wall_ms / 1000:>5.1f}s{frozen}"
            )
        )
        print(
            console.safe(
                f"  FRICTION  lookups {snap.lookup_count}"
                f"   meaning requests {snap.meaning_mode_count}"
                f"   mode {snap.mode.value}"
            )
        )

        verdicts = self.speed.page_difficulties(sid)
        if verdicts:
            last = verdicts[-1]
            because = f" - {last.evidence[0]}" if last.evidence else ""
            print(
                console.safe(
                    f"  DIFFICULTY page {last.page_index}:"
                    f" {last.difficulty.value.upper()}{because}"
                )
            )
        console.rule("-")

    # ------------------------------------------------------------------ timing

    async def read_sentences(self, count: int, *, wpm: float | None = None) -> int:
        """Read `count` sentences at the reader's own pace.

        The reader is not driven by the narrator. Each sentence takes the time its
        own word count implies at `wpm`, so a slow reader falls behind a fast
        narrator sentence by sentence — which is the drift the prediction row is
        reporting. Coupling these would make the deviation permanently zero and
        the whole module pointless.
        """

        pace = wpm or self.reader_wpm
        read = 0
        for _ in range(count):
            content = self.reading.content
            index = content.index_of(self.reading.pointer)
            if index is None:
                break
            words = content.sentences[index].word_count
            await self.clock.sleep(words / (pace / 60.0))
            if not await self.reading.advance_sentence():
                break
            read += 1
        return read

    async def wait(self, seconds: float) -> None:
        await self.clock.sleep(seconds)

    # ------------------------------------------------------------------- script

    async def run(self) -> int:
        console.banner(
            "TALETRACE - LIVE NARRATED SESSION",
            f"{self.speed_factor:g}x speed   narrator {self.narration_wpm:.0f} wpm"
            f"   reader {self.reader_wpm:.0f} wpm",
        )
        self.note(
            "WHAT YOU ARE ABOUT TO WATCH",
            "The Audio Engine narrates. The reader reads at their own pace.\n"
            "Reading Speed watches both and never touches either one.\n"
            "Every caption says which module acted and what it was allowed to do.",
        )

        await self._scene_start()
        await self._scene_drift()
        await self._scene_meaning_mode()
        await self._scene_page_turn()
        await self._scene_camera_loss()
        await self._scene_pause()
        return await self._scene_finish()

    async def _scene_start(self) -> None:
        self.note(
            "1. SESSION START",
            "Reading Engine opens the book. It starts Reading Speed's clock first,\n"
            "then hands the Audio Engine the page text. Both get the same pointer.",
        )
        await self.reading.start_session(
            profile=get_profile(self.profile_name), voice_id="fake-voice"
        )
        await self.wait(1.5)
        self.frame("The voice has started. One sentence at a time, from a queue.")

        self.note(
            "2. THE VOICE IS PLAYING",
            "The queue drains as sentences finish. 'queued' is literally how many\n"
            "plays are left on this page - the Audio Engine owns that queue and\n"
            "nothing else may reorder it.",
        )
        await self.read_sentences(2)
        self.frame("Two sentences read. Watch 'queued' fall and 'spoken' rise.")

    async def _scene_drift(self) -> None:
        self.note(
            "3. THE READER FALLS BEHIND",
            f"The narrator is fixed at {self.narration_wpm:.0f} wpm. The reader now\n"
            "slows to 70 wpm. Nothing coordinates them, so a gap opens - and that\n"
            "gap is the only thing Reading Speed is measuring.",
        )
        await self.read_sentences(3, wpm=70)
        self.frame("Reader at 70 wpm. PREDICTION now reports how far behind they are.")

    async def _scene_meaning_mode(self) -> None:
        self.note(
            "4. GESTURE SELECTS A WORD -> AI ENGINE -> MEANING MODE",
            "The reader taps a word they do not know. Two different modules react\n"
            "for two different reasons:\n"
            "  Audio Engine   stops the voice, so they are not read past\n"
            "  Reading Speed  freezes its clock, because reading a definition is\n"
            "                 not reading the page",
        )
        await self.reading.meaning_mode_on()
        await self.wait(1.0)
        self.frame("Voice PAUSED (meaning_mode). Reading clock FROZEN, wall clock is not.")

        self.note(
            "5. THE AI ENGINE ANSWERS",
            "A completed lookup reaches Reading Speed only. It is a fact about the\n"
            "reader, not an instruction to playback - so it never touches the queue.",
        )
        self.reading.lookup_completed("entropy")
        await self.wait(2.0)
        self.frame("Still frozen. Note FRICTION: the lookup was recorded as evidence.")

        await self.reading.meaning_mode_off()
        await self.wait(1.0)
        self.frame("Meaning Mode off. The voice resumed from where it stopped.")

    async def _scene_page_turn(self) -> None:
        self.note(
            "6. READING TO THE END OF THE PAGE",
            "The reader finishes the page. Crossing the boundary is what closes it,\n"
            "and closing a page is what produces a difficulty verdict.",
        )
        page = self.reading.pointer.page_index
        while self.reading.pointer.page_index == page:
            if await self.read_sentences(1) == 0:
                break
        self.frame("Page turned. The queue was rebuilt for the new page - see queue v.")
        self.note(
            "WHY THE VERDICT LOOKS LIKE THAT",
            "A slow page alone is never called hard. It needs corroborating friction\n"
            "- lookups, corrections, re-reads. Without them the honest answer is\n"
            "UNKNOWN: the reader may simply have been interrupted.",
        )

    async def _scene_camera_loss(self) -> None:
        self.note(
            "7. THE CAMERA DROPS",
            "The ESP32 stops streaming. Gesture and OCR go silent, so *observations*\n"
            "stop landing - but the reader is still reading and the clock is still\n"
            "running. The deviation grows. That is the honest picture of a blind\n"
            "session: the reader may be perfectly fine and the system simply cannot\n"
            "see them.",
        )
        await self.reading.set_camera(active=False)

        # Deliberately *not* `read_sentences`. That calls `advance_sentence`, which
        # the engine leaves working with the camera off on purpose — it is how the
        # script moves the reader at all. Using it here would move the pointer and
        # make a caption about a frozen pointer false. A blind session is the
        # reader reading while no observation lands, so what happens here is time
        # passing plus gestures that get refused.
        content = self.reading.content
        index = content.index_of(self.reading.pointer) or 0
        refused = 0
        for step in range(1, 4):
            target = content.sentences[min(index + step, content.sentence_count - 1)]
            await self.clock.sleep(target.word_count / (self.reader_wpm / 60.0))
            if not await self.reading.gesture_to(target.pointer):
                refused += 1

        self.frame(
            f"Camera off. {refused} gesture(s) refused - the pointer has not moved, "
            "but the clock has."
        )
        self.note(
            "WHY THIS MATTERS",
            "The reader read three more sentences. The system saw none of them, so\n"
            "'actual' is frozen while 'expected' keeps climbing. The deviation you\n"
            "see now is an outage, not a slow reader - and Reading Speed will refuse\n"
            "to call the page hard on the strength of it.",
        )

        await self.reading.set_camera(active=True)
        landed = await self.reading.gesture_to(
            content.sentences[min(index + 3, content.sentence_count - 1)].pointer
        )
        await self.wait(0.5)
        self.frame(
            f"Camera back. One gesture landed ({landed}) and the pointer jumped "
            "to where the reader actually was."
        )

    async def _scene_pause(self) -> None:
        self.note(
            "8. THE READER SETS THE BOOK DOWN",
            "A real pause. The voice stops and the reading clock stops with it -\n"
            "but the wall clock does not. Those two numbers diverging is what keeps\n"
            "a coffee break from being recorded as slow reading.",
        )
        await self.reading.pause()
        await self.wait(3.0)
        self.frame("Paused. Compare reading vs wall - the gap is the break.")

        await self.reading.resume()
        await self.wait(1.0)
        self.frame("Resumed. The voice picked up; the reading clock restarted.")

    async def _scene_finish(self) -> int:
        self.note(
            "9. READING OUT THE REST",
            "Straight through to the end of the book, so the summary has something\n"
            "to summarise. Watch the chapter change at page 3.",
        )
        while await self.read_sentences(1):
            pass
        self.frame("End of the book.")

        self.note(
            "10. SESSION FINISHED",
            "The Audio Engine is stopped first, because stopping is what finalises\n"
            "its statistics - and Reading Speed ingests those counts rather than\n"
            "inferring them. Summarising first would read a tally still moving.",
        )
        summary = await self.reading.finish_session()

        print()
        console.banner("SESSION SUMMARY")
        console.field("Baseline", f"{summary.baseline_wpm:.0f} wpm")
        console.field("Session pace", f"{summary.session_wpm:.0f} wpm")
        console.field("Words read", summary.words_read)
        console.field("Pages read", summary.pages_read)
        console.field("Reading time", f"{summary.reading_duration_ms / 1000:.0f}s")
        console.field("Wall time", f"{summary.wall_duration_ms / 1000:.0f}s")
        console.field("Lookups", summary.lookup_count)
        console.field("Meaning requests", summary.meaning_requests)
        console.field("Narrated", "yes" if summary.tts_assisted else "no")
        console.field(
            "Suggested baseline",
            f"{summary.suggested_baseline_wpm:.0f} wpm (suggested, not applied)"
            if summary.suggested_baseline_wpm
            else "none - evidence too thin",
        )

        print()
        console.rule("-")
        for page in summary.pages:
            print(
                console.safe(
                    f"  page {page.page_index}  {page.difficulty.value.upper():<8}"
                    f"{page.words:>4}w  expected {page.expected_ms / 1000:>5.0f}s"
                    f"  actual {page.actual_ms / 1000:>5.0f}s"
                    f"  ({page.deviation_ratio:+.0%})"
                )
            )
            for reason in page.evidence:
                print(console.safe(f"            - {reason}"))
        console.rule("-")

        stats = self.tts.final_statistics
        if stats is not None:
            print()
            console.banner("WHAT THE VOICE ACTUALLY DID")
            console.field("Sentences spoken", stats.sentences_spoken)
            console.field("Words spoken", stats.words_spoken)
            console.field("Pauses", stats.pause_count)
            console.field("Meaning Mode stops", stats.meaning_mode_count)
            console.field("Queue rebuilds", stats.queue_refreshes)
            console.field("Narration time", f"{self.sink.spoken_seconds:.0f}s of speech")
            console.field("Average pace", f"{stats.average_wpm:.0f} wpm")

        self.note(
            "WHAT THIS PROVED",
            "Reading Speed never moved the pointer, never touched the queue, never\n"
            "changed a voice. It read the session and published numbers. Delete it\n"
            "and the narration above still happens, unchanged - which is the whole\n"
            "design in one sentence.",
        )
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Narrated live TaleTrace session.")
    parser.add_argument(
        "--speed",
        type=float,
        default=4.0,
        help="Clock compression. 1 is real time; 4 (default) runs 4x faster.",
    )
    parser.add_argument(
        "--reader",
        type=float,
        default=150.0,
        help="The reader's own pace in wpm, independent of the narrator.",
    )
    parser.add_argument(
        "--profile",
        default="normal",
        help="Audio profile: normal, adaptive, disability, study, novel, exam.",
    )
    args = parser.parse_args()

    demo = LiveNarration(
        speed_factor=args.speed, reader_wpm=args.reader, profile=args.profile
    )
    return asyncio.run(demo.run())


if __name__ == "__main__":
    raise SystemExit(main())
