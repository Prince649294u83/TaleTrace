"""Reading Audio Engine tests.

Every test uses FakeSpeechProvider and NullAudioSink, so the suite runs offline
with no network, no credentials, and no audio hardware. Profiles are given
`pause_after_sentence_ms=0` so tests do not sleep through real pauses.
"""

import asyncio
import inspect
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.modules.audio_engine.audio_profiles import (
    DISABILITY,
    NORMAL,
    get_profile,
    list_profiles,
)
from backend.app.modules.audio_engine.models import (
    AudioProfile,
    PauseReason,
    PlaybackState,
    ReadingPointer,
    SentenceChunk,
    SpeechRequest,
    SpeechResponse,
    Voice,
)
from backend.app.modules.audio_engine.playback_engine import PlaybackEngine
from backend.app.modules.audio_engine.pointer_manager import PointerManager
from backend.app.modules.audio_engine.session_manager import AudioSessionManager
from backend.app.modules.audio_engine.sentence_queue import (
    SentenceQueue,
    estimate_duration_ms,
    segment_sentences,
)
from backend.app.modules.audio_engine import interfaces
from backend.app.modules.audio_engine.speech_provider import (
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

# No real pauses; keeps the suite fast.
FAST = AudioProfile(name="fast-test", rate=1.0, pause_after_sentence_ms=0)

THREE_SENTENCES = "First sentence. Second sentence. Third sentence."


def build_engine(*, provider=None, auto_advance=True):
    engine = PlaybackEngine(
        provider=provider or FakeSpeechProvider(),
        sink=NullAudioSink(),
        auto_advance=auto_advance,
    )
    engine.set_profile(FAST)
    return engine


class BlockingProvider:
    """Provider that stalls inside synthesize until released.

    Lets a test interrupt a sentence that is genuinely in flight. Records the
    text before blocking, so `spoken` reflects what playback attempted.
    """

    provider_name = "blocking"

    def __init__(self) -> None:
        self.spoken: list[str] = []
        self._speaking = asyncio.Event()
        self._release = asyncio.Event()

    async def synthesize(self, request: SpeechRequest) -> SpeechResponse:
        self.spoken.append(request.text)
        self._speaking.set()
        await self._release.wait()
        return SpeechResponse(audio=b"blocking-audio", provider=self.provider_name)

    async def get_available_voices(self) -> list[Voice]:
        return [Voice(id="blocking-voice", name="Blocking Voice")]

    async def wait_until_speaking(self, timeout: float = 2.0) -> None:
        await asyncio.wait_for(self._speaking.wait(), timeout=timeout)

    def release(self) -> None:
        """Unblock synthesis, and stay unblocked for later sentences."""

        self._release.set()


class FakeClock:
    """Manually advanced clock, so timer tests never sleep."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --------------------------- ReadingPointer ---------------------------


class TestReadingPointer:
    def test_defaults_to_page_one(self):
        p = ReadingPointer()
        assert (p.page_index, p.paragraph_index, p.sentence_index, p.character_offset) == (1, 0, 0, 0)

    def test_is_immutable(self):
        """Frozen so a consumer cannot mutate the Reading Engine's copy."""
        with pytest.raises(Exception):
            ReadingPointer().page_index = 5

    def test_next_sentence_clears_offset(self):
        p = ReadingPointer(sentence_index=2, character_offset=40).next_sentence()
        assert p.sentence_index == 3
        assert p.character_offset == 0

    def test_same_sentence_ignores_offset(self):
        a = ReadingPointer(sentence_index=3, character_offset=0)
        b = ReadingPointer(sentence_index=3, character_offset=42)
        assert a.same_sentence_as(b)

    def test_order_key_is_lexicographic(self):
        """A later paragraph must outrank an earlier one regardless of sentence index."""
        earlier = ReadingPointer(paragraph_index=0, sentence_index=9)
        later = ReadingPointer(paragraph_index=1, sentence_index=0)
        assert later.sentence_order_key() > earlier.sentence_order_key()

    def test_page_outranks_paragraph(self):
        p1 = ReadingPointer(page_index=1, paragraph_index=9, sentence_index=9)
        p2 = ReadingPointer(page_index=2, paragraph_index=0, sentence_index=0)
        assert p2.sentence_order_key() > p1.sentence_order_key()

    def test_page_index_rejects_zero(self):
        with pytest.raises(Exception):
            ReadingPointer(page_index=0)


# --------------------------- Segmentation ---------------------------


class TestSegmentation:
    def test_splits_on_terminators(self):
        chunks = segment_sentences("One. Two! Three?")
        assert [c.text for c in chunks] == ["One.", "Two!", "Three?"]

    def test_numbers_sequentially_from_pointer(self):
        chunks = segment_sentences(THREE_SENTENCES, start_pointer=ReadingPointer(sentence_index=5))
        assert [c.pointer.sentence_index for c in chunks] == [5, 6, 7]

    def test_preserves_page_and_paragraph(self):
        base = ReadingPointer(page_index=7, paragraph_index=3)
        chunks = segment_sentences(THREE_SENTENCES, start_pointer=base)
        assert all(c.pointer.page_index == 7 for c in chunks)
        assert all(c.pointer.paragraph_index == 3 for c in chunks)

    @pytest.mark.parametrize("text", ["", "   ", "\n\t"])
    def test_empty_text_yields_nothing(self, text):
        assert segment_sentences(text) == []

    def test_handles_quoted_dialogue(self):
        chunks = segment_sentences('"Stop!" she cried. He ran.')
        assert [c.text for c in chunks] == ['"Stop!" she cried.', "He ran."]

    def test_closing_quotes_are_preserved(self):
        """Quotes must survive segmentation; they carry the dialogue."""
        chunks = segment_sentences('"Run!" "Why?" "Just run!"')
        assert [c.text for c in chunks] == ['"Run!"', '"Why?"', '"Just run!"']

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("Dr. Smith arrived. He was late.", ["Dr. Smith arrived.", "He was late."]),
            (
                "J. K. Rowling wrote it. She was famous.",
                ["J. K. Rowling wrote it.", "She was famous."],
            ),
            ("Read Vol. 3 tonight. Then sleep.", ["Read Vol. 3 tonight.", "Then sleep."]),
        ],
    )
    def test_abbreviations_do_not_split(self, text, expected):
        """A pause after 'Dr.' is audibly wrong, so abbreviations stay joined."""
        assert [c.text for c in segment_sentences(text)] == expected

    def test_decimals_do_not_split(self):
        chunks = segment_sentences("It cost $5.50. That was too much.")
        assert [c.text for c in chunks] == ["It cost $5.50.", "That was too much."]

    def test_ellipsis_stays_in_one_sentence(self):
        chunks = segment_sentences("She paused... then spoke. The room fell silent.")
        assert [c.text for c in chunks] == [
            "She paused... then spoke.",
            "The room fell silent.",
        ]

    def test_no_terminator_is_one_sentence(self):
        assert len(segment_sentences("no terminator here")) == 1

    def test_duration_scales_inversely_with_rate(self):
        text = " ".join(["word"] * 150)
        assert estimate_duration_ms(text, rate=0.5) > estimate_duration_ms(text, rate=1.0)

    def test_duration_of_empty_is_zero(self):
        assert estimate_duration_ms("", rate=1.0) == 0


# --------------------------- SentenceQueue ---------------------------


class TestSentenceQueue:
    def _chunks(self):
        return segment_sentences(THREE_SENTENCES)

    def test_fifo_order(self):
        q = SentenceQueue()
        q.extend(self._chunks())
        assert q.dequeue().text == "First sentence."
        assert q.dequeue().text == "Second sentence."

    def test_peek_does_not_consume(self):
        q = SentenceQueue()
        q.extend(self._chunks())
        assert q.peek().text == q.peek().text
        assert q.size() == 3

    def test_dequeue_when_empty(self):
        assert SentenceQueue().dequeue() is None

    def test_push_front_restores_order(self):
        q = SentenceQueue()
        q.extend(self._chunks())
        first = q.dequeue()
        q.push_front(first)
        assert q.dequeue().text == "First sentence."

    def test_replace_after_keeps_only_later_sentences(self):
        q = SentenceQueue()
        q.replace_after(ReadingPointer(sentence_index=0), self._chunks())
        assert [c.text for c in list(q._items)] == ["Second sentence.", "Third sentence."]

    def test_replace_after_across_paragraphs(self):
        """Sentence 0 of the next paragraph must survive an anchor late in this one."""
        chunks = [
            SentenceChunk(text="a", pointer=ReadingPointer(paragraph_index=0, sentence_index=9)),
            SentenceChunk(text="b", pointer=ReadingPointer(paragraph_index=1, sentence_index=0)),
        ]
        q = SentenceQueue()
        q.replace_after(ReadingPointer(paragraph_index=0, sentence_index=5), chunks)
        assert [c.text for c in list(q._items)] == ["a", "b"]

    def test_drop_before_is_inclusive_of_target(self):
        q = SentenceQueue()
        q.extend(self._chunks())
        q.drop_before(ReadingPointer(sentence_index=1))
        assert [c.text for c in list(q._items)] == ["Second sentence.", "Third sentence."]


# --------------------------- State machine ---------------------------


class TestStateMachine:
    def test_starts_idle(self):
        assert PlaybackStateMachine().state is PlaybackState.IDLE

    def test_happy_path(self):
        m = PlaybackStateMachine()
        for target in (
            PlaybackState.READY,
            PlaybackState.PLAYING,
            PlaybackState.PAUSED,
            PlaybackState.PLAYING,
            PlaybackState.FINISHED,
        ):
            m.transition_to(target)
        assert m.state is PlaybackState.FINISHED

    def test_illegal_transition_raises(self):
        m = PlaybackStateMachine()
        with pytest.raises(InvalidTransition):
            m.transition_to(PlaybackState.PLAYING)  # IDLE -> PLAYING skips READY

    def test_reentering_state_is_noop(self):
        """Repeated pause/stop calls must be safe."""
        m = PlaybackStateMachine()
        m.transition_to(PlaybackState.READY)
        assert m.transition_to(PlaybackState.READY) is PlaybackState.READY

    def test_pause_reason_recorded_and_cleared(self):
        m = PlaybackStateMachine()
        m.transition_to(PlaybackState.READY)
        m.transition_to(PlaybackState.PLAYING)
        m.transition_to(PlaybackState.PAUSED, pause_reason=PauseReason.MEANING_MODE)
        assert m.pause_reason is PauseReason.MEANING_MODE
        m.transition_to(PlaybackState.PLAYING)
        assert m.pause_reason is None

    def test_timer_freezes_while_paused(self):
        """Meaning Mode must not inflate reading-speed analytics."""
        clock = FakeClock()
        m = PlaybackStateMachine(clock=clock)
        m.transition_to(PlaybackState.READY)
        m.transition_to(PlaybackState.PLAYING)
        clock.advance(2.0)
        m.transition_to(PlaybackState.PAUSED, pause_reason=PauseReason.MEANING_MODE)

        clock.advance(60.0)  # a long lookup
        assert m.elapsed_reading_ms == pytest.approx(2000, abs=50)

        m.transition_to(PlaybackState.PLAYING)
        clock.advance(1.0)
        assert m.elapsed_reading_ms == pytest.approx(3000, abs=50)

    def test_timer_runs_while_waiting_for_pointer(self):
        """The reader is still reading during a Reading Update."""
        clock = FakeClock()
        m = PlaybackStateMachine(clock=clock)
        m.transition_to(PlaybackState.READY)
        m.transition_to(PlaybackState.PLAYING)
        m.transition_to(PlaybackState.WAITING_FOR_POINTER)
        clock.advance(3.0)
        assert m.elapsed_reading_ms == pytest.approx(3000, abs=50)

    def test_timer_resets_on_idle(self):
        clock = FakeClock()
        m = PlaybackStateMachine(clock=clock)
        m.transition_to(PlaybackState.READY)
        m.transition_to(PlaybackState.PLAYING)
        clock.advance(5.0)
        m.transition_to(PlaybackState.IDLE)
        assert m.elapsed_reading_ms == 0


# --------------------------- Pointer manager ---------------------------


class TestPointerManager:
    def test_defaults_before_set(self):
        pm = PointerManager()
        assert pm.is_set is False
        assert pm.current_pointer() == ReadingPointer()

    def test_advance_increments_sentence(self):
        pm = PointerManager(ReadingPointer(sentence_index=1))
        assert pm.advance().sentence_index == 2

    def test_reset_clears(self):
        pm = PointerManager(ReadingPointer(sentence_index=4))
        pm.reset()
        assert pm.is_set is False


# --------------------------- Profiles ---------------------------


class TestProfiles:
    def test_disability_is_slower_with_longer_pauses(self):
        assert DISABILITY.rate < NORMAL.rate
        assert DISABILITY.pause_after_sentence_ms > NORMAL.pause_after_sentence_ms

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("adaptive", "adaptive"),
            ("ADAPTIVE", "adaptive"),
            ("  disability  ", "disability"),
            ("standard", "normal"),   # AI Engine reading mode
            ("dyslexic", "disability"),  # alias
            ("nonsense", "normal"),
            (None, "normal"),
            ("", "normal"),
        ],
    )
    def test_resolution_and_fallback(self, name, expected):
        assert get_profile(name).name == expected

    def test_ai_engine_reading_modes_all_resolve(self):
        """Every AI Engine ReadingMode must map to some profile."""
        from backend.app.modules.ai_engine.models import ReadingMode

        for mode in ReadingMode:
            assert get_profile(mode.value) is not None

    def test_profiles_are_immutable(self):
        with pytest.raises(Exception):
            NORMAL.rate = 2.0

    def test_list_profiles_nonempty(self):
        assert len(list_profiles()) >= 5


# --------------------------- Providers ---------------------------


class TestProviders:
    @pytest.mark.asyncio
    async def test_fake_records_spoken_text(self):
        engine = build_engine()
        await engine.start(pointer=ReadingPointer(), text="One. Two.", profile=FAST)
        await engine.wait_for_idle()
        assert engine._provider.spoken == ["One.", "Two."]

    @pytest.mark.asyncio
    async def test_synthesis_failure_surfaces_without_crashing(self):
        engine = build_engine(provider=FakeSpeechProvider(fail=True))
        await engine.start(pointer=ReadingPointer(), text="One. Two.", profile=FAST)
        await engine.wait_for_idle()
        status = engine.get_status()
        assert status.error is not None
        assert status.state is PlaybackState.FINISHED

    def test_get_provider_by_name(self):
        assert get_provider("offline").provider_name == "offline"
        assert get_provider("edge").provider_name == "edge"

    def test_unknown_provider_falls_back_to_edge(self):
        assert get_provider("nonexistent").provider_name == "edge"

    @pytest.mark.asyncio
    async def test_swapping_provider_changes_nothing_upstream(self):
        """The whole point of the abstraction."""
        results = []
        for provider in (FakeSpeechProvider(), FakeSpeechProvider()):
            engine = build_engine(provider=provider)
            await engine.start(pointer=ReadingPointer(), text=THREE_SENTENCES, profile=FAST)
            await engine.wait_for_idle()
            results.append(engine.get_status().state)
        assert results == [PlaybackState.FINISHED, PlaybackState.FINISHED]


# --------------------------- Playback engine ---------------------------


class TestPlaybackEngine:
    @pytest.mark.asyncio
    async def test_plays_all_sentences_then_finishes(self):
        engine = build_engine()
        await engine.start(pointer=ReadingPointer(), text=THREE_SENTENCES, profile=FAST)
        await engine.wait_for_idle()
        assert engine._provider.spoken == ["First sentence.", "Second sentence.", "Third sentence."]
        assert engine.get_status().state is PlaybackState.FINISHED

    @pytest.mark.asyncio
    async def test_empty_text_finishes_immediately(self):
        engine = build_engine()
        state = await engine.start(pointer=ReadingPointer(), text="", profile=FAST)
        assert state is PlaybackState.FINISHED

    @pytest.mark.asyncio
    async def test_starting_mid_paragraph_skips_earlier_sentences(self):
        """Pointer at sentence 1 must not replay sentence 0."""
        engine = build_engine()
        await engine.start(
            pointer=ReadingPointer(sentence_index=1), text=THREE_SENTENCES, profile=FAST
        )
        await engine.wait_for_idle()
        assert engine._provider.spoken == ["Second sentence.", "Third sentence."]

    @pytest.mark.asyncio
    async def test_pause_preserves_current_sentence(self):
        """Resume must re-speak the interrupted sentence, not skip it.

        The pause has to land while a sentence is genuinely in flight, which is
        what BlockingProvider arranges. Pausing after a sentence has finished is
        a different case: there is nothing to preserve and advancing is correct
        (covered by test_pause_after_sentence_completes_advances).
        """
        provider = BlockingProvider()
        engine = build_engine(provider=provider)
        await engine.start(pointer=ReadingPointer(), text=THREE_SENTENCES, profile=FAST)

        # Wait until the first sentence is mid-synthesis, then interrupt it.
        await provider.wait_until_speaking()
        assert provider.spoken == ["First sentence."]

        await engine.pause(reason=PauseReason.MEANING_MODE)
        assert engine.get_status().state is PlaybackState.PAUSED
        assert engine.get_status().pause_reason is PauseReason.MEANING_MODE

        provider.release()
        await engine.resume()
        await engine.wait_for_idle()

        # 'First sentence.' is spoken twice: once interrupted, once in full.
        assert provider.spoken == [
            "First sentence.",
            "First sentence.",
            "Second sentence.",
            "Third sentence.",
        ]

    @pytest.mark.asyncio
    async def test_pause_after_sentence_completes_advances(self):
        """A sentence that finished is not replayed; resume moves on."""
        engine = build_engine(auto_advance=False)
        await engine.start(pointer=ReadingPointer(), text=THREE_SENTENCES, profile=FAST)
        await engine.wait_for_idle()

        await engine.pause()
        assert engine.get_status().state is PlaybackState.PAUSED

        await engine.resume()
        await engine.wait_for_idle()
        assert engine._provider.spoken == ["First sentence.", "Second sentence."]

    @pytest.mark.asyncio
    async def test_pause_when_not_playing_is_noop(self):
        engine = build_engine()
        assert await engine.pause() is PlaybackState.IDLE

    @pytest.mark.asyncio
    async def test_resume_when_not_paused_is_noop(self):
        engine = build_engine()
        assert await engine.resume() is PlaybackState.IDLE

    @pytest.mark.asyncio
    async def test_stop_clears_everything(self):
        engine = build_engine(auto_advance=False)
        await engine.start(pointer=ReadingPointer(), text=THREE_SENTENCES, profile=FAST)
        await engine.stop()
        status = engine.get_status()
        assert status.state is PlaybackState.IDLE
        assert status.queued_sentences == 0
        assert status.pointer is None

    @pytest.mark.asyncio
    async def test_seek_while_idle_applies_immediately(self):
        engine = build_engine()
        await engine.seek(pointer=ReadingPointer(sentence_index=4), text=None)
        assert engine.get_status().pointer.sentence_index == 4

    @pytest.mark.asyncio
    async def test_seek_without_text_does_not_replay_earlier_sentences(self):
        """Regression: a stale queued sentence must not undo the jump."""
        engine = build_engine(auto_advance=False)
        await engine.start(pointer=ReadingPointer(), text=THREE_SENTENCES, profile=FAST)
        await engine.wait_for_idle()

        await engine.pause()
        await engine.seek(pointer=ReadingPointer(sentence_index=2))
        spoken_before = len(engine._provider.spoken)
        await engine.resume()
        await engine.wait_for_idle()

        newly_spoken = engine._provider.spoken[spoken_before:]
        assert "First sentence." not in newly_spoken

    @pytest.mark.asyncio
    async def test_seek_after_page_ran_dry_restarts_playback(self):
        """Regression: a gesture on a finished page must not kill narration.

        Found on a real photograph. OCR split the page so that the paragraph the
        session opened on was a single word — routine, since the reader's hand
        cuts a line down to a fragment. Narration spoke it, the queue emptied and
        the engine reached FINISHED. The reader then pointed at a word further
        down the *same* page, which the Reading Engine routes to `seek()`.

        The queue refilled and nothing ever spoke it: the loop had already
        returned, and only `start()` and `resume()` spawn a new one. Narration was
        dead until a page turn. FINISHED means "nothing left to speak", not "the
        reader has left this page".
        """
        provider = FakeSpeechProvider()
        engine = build_engine(provider=provider)

        await engine.start(pointer=ReadingPointer(), text="for", profile=FAST)
        await engine.wait_for_idle()
        assert engine.get_status().state is PlaybackState.FINISHED

        await engine.seek(
            pointer=ReadingPointer(paragraph_index=20),
            text="Two topics impact everyone. They are health and money.",
        )
        await engine.wait_for_idle()

        assert provider.spoken == [
            "for",
            "Two topics impact everyone.",
            "They are health and money.",
        ]
        assert engine.get_status().queued_sentences == 0

    @pytest.mark.asyncio
    async def test_seek_after_finish_without_new_text_stays_finished(self):
        """Reviving requires sentences. An empty seek must not spin a loop."""
        engine = build_engine()
        await engine.start(pointer=ReadingPointer(), text="Only one.", profile=FAST)
        await engine.wait_for_idle()

        await engine.seek(pointer=ReadingPointer(sentence_index=3))
        assert engine.get_status().state is PlaybackState.FINISHED

    @pytest.mark.asyncio
    async def test_seek_does_not_resume_a_paused_reader(self):
        """Only FINISHED revives. A deliberate pause is the reader's to undo."""
        engine = build_engine(auto_advance=False)
        await engine.start(pointer=ReadingPointer(), text=THREE_SENTENCES, profile=FAST)
        await engine.wait_for_idle()
        await engine.pause(reason=PauseReason.MEANING_MODE)

        await engine.seek(
            pointer=ReadingPointer(paragraph_index=2), text="A wholly new paragraph."
        )

        status = engine.get_status()
        assert status.state is PlaybackState.PAUSED
        assert status.pause_reason is PauseReason.MEANING_MODE

    @pytest.mark.asyncio
    async def test_revived_playback_keeps_the_pages_reading_time(self):
        """The revive goes through READY, which zeroes the machine's clock."""
        engine = build_engine()
        await engine.start(pointer=ReadingPointer(), text="for", profile=FAST)
        await engine.wait_for_idle()
        banked = engine.get_status().statistics.reading_time_ms

        await engine.seek(
            pointer=ReadingPointer(paragraph_index=20), text="Two topics impact everyone."
        )
        await engine.wait_for_idle()

        assert engine.get_status().statistics.reading_time_ms >= banked

    @pytest.mark.asyncio
    async def test_refresh_queue_does_not_restart_current_sentence(self):
        """Merge updates must never interrupt what is being spoken."""
        engine = build_engine(auto_advance=False)
        await engine.start(pointer=ReadingPointer(), text=THREE_SENTENCES, profile=FAST)
        await engine.wait_for_idle()

        spoken_before = list(engine._provider.spoken)
        await engine.refresh_queue(text="First sentence. Second CORRECTED. Third CORRECTED.")

        assert engine._provider.spoken == spoken_before  # nothing re-spoken

        await engine.resume() if engine.get_status().state is PlaybackState.PAUSED else None
        engine._auto_advance = True
        await engine.start(
            pointer=ReadingPointer(sentence_index=1),
            text="First sentence. Second CORRECTED. Third CORRECTED.",
            profile=FAST,
        )
        await engine.wait_for_idle()
        assert "Second CORRECTED." in engine._provider.spoken

    @pytest.mark.asyncio
    async def test_refresh_queue_without_text_is_noop(self):
        engine = build_engine(auto_advance=False)
        await engine.start(pointer=ReadingPointer(), text=THREE_SENTENCES, profile=FAST)
        await engine.refresh_queue()
        assert engine.get_status().state in (PlaybackState.PLAYING, PlaybackState.FINISHED)

    @pytest.mark.asyncio
    async def test_page_turn_resets_pointer(self):
        engine = build_engine()
        await engine.start(pointer=ReadingPointer(page_index=1), text=THREE_SENTENCES, profile=FAST)
        await engine.wait_for_idle()

        await engine.start(
            pointer=ReadingPointer(page_index=2), text="New page here.", profile=FAST
        )
        await engine.wait_for_idle()
        assert engine.get_status().pointer.page_index == 2

    @pytest.mark.asyncio
    async def test_restart_cancels_previous_loop(self):
        """An old loop must not keep speaking from a replaced queue."""
        engine = build_engine()
        await engine.start(pointer=ReadingPointer(), text=THREE_SENTENCES, profile=FAST)
        await engine.start(pointer=ReadingPointer(), text="Only this.", profile=FAST)
        await engine.wait_for_idle()
        assert engine._provider.spoken[-1] == "Only this."

    @pytest.mark.asyncio
    async def test_profile_change_applies_to_later_sentences(self):
        engine = build_engine(auto_advance=False)
        await engine.start(pointer=ReadingPointer(), text=THREE_SENTENCES, profile=FAST)
        engine.set_profile(DISABILITY)
        assert engine.get_status().profile_name == "disability"

    @pytest.mark.asyncio
    async def test_status_reports_provider_and_queue_depth(self):
        engine = build_engine(auto_advance=False)
        await engine.start(pointer=ReadingPointer(), text=THREE_SENTENCES, profile=FAST)
        status = engine.get_status()
        assert status.provider == "fake"
        assert status.queued_sentences >= 0


# --------------------------- Queue versioning ---------------------------


class TestQueueVersioning:
    def test_version_starts_at_zero(self):
        assert SentenceQueue().version == 0

    def test_mutations_bump_version(self):
        queue = SentenceQueue()
        chunks = segment_sentences(THREE_SENTENCES)
        seen = [queue.version]

        queue.extend(chunks)
        seen.append(queue.version)
        queue.push_front(chunks[0])
        seen.append(queue.version)
        queue.replace(chunks)
        seen.append(queue.version)
        queue.clear()
        seen.append(queue.version)

        assert seen == sorted(set(seen)), f"version must increase monotonically: {seen}"

    def test_dequeue_does_not_bump_version(self):
        """Consuming a sentence is not a rewrite of what is pending."""
        queue = SentenceQueue()
        queue.extend(segment_sentences(THREE_SENTENCES))
        before = queue.version
        queue.dequeue()
        assert queue.version == before

    def test_noop_mutations_do_not_bump(self):
        queue = SentenceQueue()
        before = queue.version
        queue.clear()
        queue.extend([])
        assert queue.version == before

    def test_drop_before_reports_count(self):
        queue = SentenceQueue()
        queue.extend(segment_sentences(THREE_SENTENCES))
        assert queue.drop_before(ReadingPointer(sentence_index=2)) == 2
        assert queue.size() == 1

    @pytest.mark.asyncio
    async def test_stale_refresh_is_rejected(self):
        """An OCR frame arriving after a newer one must not regress the text."""
        engine = build_engine(auto_advance=False)
        await engine.start(pointer=ReadingPointer(), text=THREE_SENTENCES, profile=FAST)

        assert await engine.refresh_queue(text="Newer text here. And more.", source_version=5)
        version_after_fresh = engine.get_status().queue_version

        # Version 3 was produced before 5, so it is stale.
        assert not await engine.refresh_queue(text="Older text.", source_version=3)
        assert engine.get_status().queue_version == version_after_fresh
        assert engine.get_status().statistics.stale_updates_rejected == 1

    @pytest.mark.asyncio
    async def test_equal_source_version_is_rejected(self):
        """Re-delivery of the same frame is a duplicate, not an update."""
        engine = build_engine(auto_advance=False)
        await engine.start(pointer=ReadingPointer(), text=THREE_SENTENCES, profile=FAST)

        assert await engine.refresh_queue(text="A. B.", source_version=2)
        assert not await engine.refresh_queue(text="A. B.", source_version=2)

    @pytest.mark.asyncio
    async def test_refresh_without_version_always_applies(self):
        """Omitting the version keeps the simple single-caller path working."""
        engine = build_engine(auto_advance=False)
        await engine.start(pointer=ReadingPointer(), text=THREE_SENTENCES, profile=FAST)
        assert await engine.refresh_queue(text="One. Two.")
        assert await engine.refresh_queue(text="Three. Four.")

    @pytest.mark.asyncio
    async def test_refresh_with_no_content_is_ignored(self):
        engine = build_engine(auto_advance=False)
        await engine.start(pointer=ReadingPointer(), text=THREE_SENTENCES, profile=FAST)
        assert not await engine.refresh_queue()


# --------------------------- Statistics ---------------------------


class TestStatistics:
    @pytest.mark.asyncio
    async def test_counts_only_completed_sentences(self):
        engine = build_engine()
        await engine.start(pointer=ReadingPointer(), text=THREE_SENTENCES, profile=FAST)
        await engine.wait_for_idle()

        stats = engine.get_status().statistics
        assert stats.sentences_spoken == 3
        assert stats.words_spoken == 6
        # Sum of the sentences themselves, without the spaces between them.
        expected = sum(len(c.text) for c in segment_sentences(THREE_SENTENCES))
        assert stats.characters_spoken == expected

    @pytest.mark.asyncio
    async def test_interrupted_sentence_counted_once(self):
        """A sentence cut off and re-spoken must not be counted twice."""
        provider = BlockingProvider()
        engine = build_engine(provider=provider)
        await engine.start(pointer=ReadingPointer(), text=THREE_SENTENCES, profile=FAST)

        await provider.wait_until_speaking()
        await engine.pause(reason=PauseReason.MEANING_MODE)
        # Spoken twice by the provider, but only completed once.
        assert provider.spoken == ["First sentence."]
        assert engine.get_status().statistics.sentences_spoken == 0

        provider.release()
        await engine.resume()
        await engine.wait_for_idle()
        assert engine.get_status().statistics.sentences_spoken == 3

    @pytest.mark.asyncio
    async def test_counts_pauses_and_meaning_mode(self):
        engine = build_engine(auto_advance=False)
        await engine.start(pointer=ReadingPointer(), text=THREE_SENTENCES, profile=FAST)

        await engine.pause(reason=PauseReason.MEANING_MODE)
        await engine.resume()
        await engine.pause(reason=PauseReason.USER)

        stats = engine.get_status().statistics
        assert stats.pause_count == 2
        assert stats.meaning_mode_count == 1

    @pytest.mark.asyncio
    async def test_counts_reading_updates_and_refreshes(self):
        engine = build_engine(auto_advance=False)
        await engine.start(pointer=ReadingPointer(), text=THREE_SENTENCES, profile=FAST)

        await engine.seek(pointer=ReadingPointer(sentence_index=1))
        await engine.refresh_queue(text="New text. More text.")

        stats = engine.get_status().statistics
        assert stats.reading_updates == 1
        assert stats.queue_refreshes == 1

    @pytest.mark.asyncio
    async def test_counts_distinct_pages(self):
        engine = build_engine()
        await engine.start(
            pointer=ReadingPointer(page_index=1), text=THREE_SENTENCES, profile=FAST
        )
        await engine.wait_for_idle()
        await engine.seek(pointer=ReadingPointer(page_index=2), text=THREE_SENTENCES)
        await engine.wait_for_idle()

        assert engine.get_status().statistics.pages_read == 2

    @pytest.mark.asyncio
    async def test_reading_time_excludes_pauses(self):
        """Meaning Mode must not inflate reading-speed analytics."""
        clock = FakeClock()
        engine = PlaybackEngine(
            provider=FakeSpeechProvider(),
            sink=NullAudioSink(),
            auto_advance=False,
            clock=clock,
        )
        engine.set_profile(FAST)
        await engine.start(pointer=ReadingPointer(), text=THREE_SENTENCES, profile=FAST)

        clock.advance(2.0)
        await engine.pause(reason=PauseReason.MEANING_MODE)
        clock.advance(60.0)  # A long lookup.
        await engine.resume()

        stats = engine.get_status().statistics
        assert 1900 <= stats.reading_time_ms <= 2100
        # Wall-clock playback time does include the pause.
        assert stats.playback_time_ms >= 62_000

    @pytest.mark.asyncio
    async def test_average_wpm_uses_reading_time(self):
        clock = FakeClock()
        engine = PlaybackEngine(
            provider=FakeSpeechProvider(),
            sink=NullAudioSink(),
            auto_advance=True,
            clock=clock,
        )
        engine.set_profile(FAST)
        await engine.start(pointer=ReadingPointer(), text=THREE_SENTENCES, profile=FAST)
        await engine.wait_for_idle()
        clock.advance(60.0)

        stats = engine.get_status().statistics
        # 6 words; the clock froze at FINISHED, so elapsed stays near zero and
        # wpm must not be computed against the 60s of idle time.
        assert stats.words_spoken == 6
        assert stats.average_wpm >= 0

    @pytest.mark.asyncio
    async def test_final_statistics_survive_stop(self):
        """stop() resets the reading clock, so the summary is snapshotted first."""
        clock = FakeClock()
        engine = PlaybackEngine(
            provider=FakeSpeechProvider(),
            sink=NullAudioSink(),
            auto_advance=False,
            clock=clock,
        )
        engine.set_profile(FAST)
        await engine.start(pointer=ReadingPointer(), text=THREE_SENTENCES, profile=FAST)
        clock.advance(30.0)

        live_before = engine.get_status().statistics
        assert live_before.reading_time_ms >= 30_000

        await engine.stop()

        # Going IDLE zeroes the machine's clock, so the live view loses it...
        assert engine.get_status().statistics.reading_time_ms == 0
        # ...but the snapshot taken inside stop() still has it, which is what a
        # session summary needs.
        assert engine.final_statistics is not None
        assert engine.final_statistics.reading_time_ms >= 30_000

    @pytest.mark.asyncio
    async def test_page_turn_accumulates_statistics(self):
        """A mid-session start() is a page turn, so the tally carries over.

        Found by the simulator: resetting here silently zeroed a reader's
        analytics at every page turn.
        """
        engine = build_engine()
        await engine.start(pointer=ReadingPointer(page_index=1), text=THREE_SENTENCES, profile=FAST)
        await engine.wait_for_idle()
        await engine.start(pointer=ReadingPointer(page_index=2), text="Only one.", profile=FAST)
        await engine.wait_for_idle()

        stats = engine.get_status().statistics
        assert stats.sentences_spoken == 4
        assert stats.pages_read == 2

    @pytest.mark.asyncio
    async def test_start_after_stop_resets_statistics(self):
        """stop() ends the session, so the next start() begins a fresh tally."""
        engine = build_engine()
        await engine.start(pointer=ReadingPointer(), text=THREE_SENTENCES, profile=FAST)
        await engine.wait_for_idle()
        await engine.stop()

        await engine.start(pointer=ReadingPointer(), text="Only one.", profile=FAST)
        await engine.wait_for_idle()

        assert engine.get_status().statistics.sentences_spoken == 1

    @pytest.mark.asyncio
    async def test_page_turn_preserves_reading_time(self):
        """The machine's clock is zeroed by a page turn; the tally must not be."""
        clock = FakeClock()
        engine = PlaybackEngine(
            provider=FakeSpeechProvider(),
            sink=NullAudioSink(),
            auto_advance=False,
            clock=clock,
        )
        engine.set_profile(FAST)
        await engine.start(pointer=ReadingPointer(page_index=1), text=THREE_SENTENCES, profile=FAST)
        clock.advance(20.0)

        await engine.start(pointer=ReadingPointer(page_index=2), text=THREE_SENTENCES, profile=FAST)
        clock.advance(10.0)

        assert engine.get_status().statistics.reading_time_ms >= 30_000


# --------------------------- Audio sink ---------------------------


class TestLocalAudioSink:
    """The sink is what makes playback take real time.

    When it silently discards audio, every sentence returns instantly and the
    engine drains its queue before any event can interrupt it — which is how a
    realtime rehearsal ends up reporting zero pauses.
    """

    def test_prefers_a_command_line_player_when_present(self, monkeypatch):
        monkeypatch.setattr(
            "backend.app.modules.audio_engine.speech_provider.shutil.which",
            lambda name: "/usr/bin/ffplay" if name == "ffplay" else None,
        )
        command = LocalAudioSink()._build_command(Path("/tmp/a.mp3"))

        assert command is not None
        assert command[0] == "ffplay"
        assert str(Path("/tmp/a.mp3")) in command

    def test_falls_back_to_powershell(self, monkeypatch):
        """Windows ships no command-line audio player."""

        monkeypatch.setattr(
            "backend.app.modules.audio_engine.speech_provider.shutil.which",
            lambda name: r"C:\Windows\powershell.exe" if name == "powershell" else None,
        )
        command = LocalAudioSink()._build_command(Path(r"C:\tmp\a.mp3"))

        assert command is not None
        assert command[0] == "powershell"
        script = command[-1]
        assert r"C:\tmp\a.mp3" in script
        # It must wait for the media to finish, or playback is fire-and-forget.
        assert "NaturalDuration" in script
        assert "__TALETRACE_AUDIO_PATH__" not in script

    def test_escapes_quotes_in_the_path(self, monkeypatch):
        """A quote in a temp path must not terminate the PowerShell string."""

        monkeypatch.setattr(
            "backend.app.modules.audio_engine.speech_provider.shutil.which",
            lambda name: "powershell" if name == "powershell" else None,
        )
        command = LocalAudioSink()._build_command(Path("/tmp/it's here.mp3"))

        assert command is not None
        assert "it''s here.mp3" in command[-1]

    def test_returns_none_when_nothing_can_play(self, monkeypatch):
        monkeypatch.setattr(
            "backend.app.modules.audio_engine.speech_provider.shutil.which",
            lambda name: None,
        )
        assert LocalAudioSink()._build_command(Path("/tmp/a.mp3")) is None

    @pytest.mark.asyncio
    async def test_empty_audio_never_spawns_a_process(self, monkeypatch):
        def explode(*args, **kwargs):
            raise AssertionError("should not spawn a player for empty audio")

        monkeypatch.setattr(
            "backend.app.modules.audio_engine.speech_provider.subprocess.Popen", explode
        )
        await LocalAudioSink().play(b"")


# --------------------------- Interface conformance ---------------------------


class TestInterfaceConformance:
    """Keep `interfaces.py` honest.

    The Protocols are what the Reading Engine will code against, and nothing
    executes them — a stale one is indistinguishable from a correct one until
    integration fails. These tests compare each Protocol against its real
    implementation so drift breaks the suite instead of the integration.

    Parameter names and defaults are checked, but not return annotations:
    returning the concrete `PlaybackEngine` where the Protocol declares
    `PlaybackEngineInterface` is correct covariance, not drift.
    """

    @staticmethod
    def _methods(protocol) -> list[str]:
        return [
            name
            for name, member in vars(protocol).items()
            if not name.startswith("_") and (callable(member) or isinstance(member, property))
        ]

    @pytest.mark.parametrize(
        "protocol, implementation",
        [
            (interfaces.SpeechProviderInterface, FakeSpeechProvider),
            (interfaces.SpeechProviderInterface, EdgeSpeechProvider),
            (interfaces.SpeechProviderInterface, OfflineSpeechProvider),
            (interfaces.AudioSinkInterface, NullAudioSink),
            (interfaces.AudioSinkInterface, LocalAudioSink),
            (interfaces.PlaybackEngineInterface, PlaybackEngine),
            (interfaces.PointerManagerInterface, PointerManager),
            (interfaces.SentenceQueueInterface, SentenceQueue),
            (interfaces.AudioSessionManagerInterface, AudioSessionManager),
        ],
    )
    def test_implementation_satisfies_protocol(self, protocol, implementation):
        missing = [
            name for name in self._methods(protocol) if not hasattr(implementation, name)
        ]
        assert not missing, (
            f"{implementation.__name__} is missing {missing} "
            f"declared by {protocol.__name__}"
        )

    @pytest.mark.parametrize(
        "protocol, implementation",
        [
            (interfaces.SpeechProviderInterface, FakeSpeechProvider),
            (interfaces.AudioSinkInterface, LocalAudioSink),
            (interfaces.PlaybackEngineInterface, PlaybackEngine),
            (interfaces.PointerManagerInterface, PointerManager),
            (interfaces.SentenceQueueInterface, SentenceQueue),
            (interfaces.AudioSessionManagerInterface, AudioSessionManager),
        ],
    )
    def test_signatures_match_protocol(self, protocol, implementation):
        for name in self._methods(protocol):
            declared = getattr(protocol, name)
            actual = getattr(implementation, name)
            if isinstance(declared, property) or isinstance(actual, property):
                continue

            expected_params = inspect.signature(declared).parameters
            actual_params = inspect.signature(actual).parameters

            assert list(expected_params) == list(actual_params), (
                f"{implementation.__name__}.{name} parameters "
                f"{list(actual_params)} do not match {protocol.__name__} "
                f"{list(expected_params)}"
            )
            for param, expected in expected_params.items():
                assert expected.default == actual_params[param].default, (
                    f"{implementation.__name__}.{name} parameter {param!r} default "
                    f"{actual_params[param].default!r} does not match "
                    f"{protocol.__name__} default {expected.default!r}"
                )

    def test_refresh_queue_reports_whether_it_applied(self):
        """The return type documented in `interfaces.py` is the one callers branch on."""

        annotation = inspect.signature(PlaybackEngine.refresh_queue).return_annotation
        assert annotation is bool


# --------------------------- Session isolation ---------------------------


class TestSessionManager:
    def _manager(self):
        def factory(session_id):
            engine = PlaybackEngine(
                provider=FakeSpeechProvider(),
                sink=NullAudioSink(),
                auto_advance=False,
                session_id=session_id,
            )
            engine.set_profile(FAST)
            return engine

        return AudioSessionManager(engine_factory=factory)

    def test_same_id_returns_same_engine(self):
        manager = self._manager()
        assert manager.get("reader-1") is manager.get("reader-1")

    def test_different_ids_get_different_engines(self):
        manager = self._manager()
        assert manager.get("reader-1") is not manager.get("reader-2")

    def test_engine_knows_its_session_id(self):
        manager = self._manager()
        assert manager.get("reader-1").session_id == "reader-1"
        assert manager.get("reader-1").get_status().session_id == "reader-1"

    @pytest.mark.asyncio
    async def test_pausing_one_session_leaves_the_other_playing(self):
        """The bug this whole layer exists to prevent."""
        manager = self._manager()
        a, b = manager.get("reader-a"), manager.get("reader-b")

        await a.start(pointer=ReadingPointer(), text=THREE_SENTENCES, profile=FAST)
        await b.start(pointer=ReadingPointer(), text=THREE_SENTENCES, profile=FAST)
        await a.pause(reason=PauseReason.MEANING_MODE)

        assert a.get_status().state is PlaybackState.PAUSED
        assert b.get_status().state is PlaybackState.PLAYING

    @pytest.mark.asyncio
    async def test_sessions_keep_separate_pointers(self):
        manager = self._manager()
        a, b = manager.get("reader-a"), manager.get("reader-b")

        await a.start(pointer=ReadingPointer(page_index=1), text=THREE_SENTENCES, profile=FAST)
        await b.start(pointer=ReadingPointer(page_index=9), text=THREE_SENTENCES, profile=FAST)

        assert a.get_status().pointer.page_index == 1
        assert b.get_status().pointer.page_index == 9

    @pytest.mark.asyncio
    async def test_close_removes_and_stops(self):
        manager = self._manager()
        engine = manager.get("reader-1")
        await engine.start(pointer=ReadingPointer(), text=THREE_SENTENCES, profile=FAST)

        assert await manager.close("reader-1") is True
        assert manager.has("reader-1") is False
        assert engine.get_status().state is PlaybackState.IDLE

    @pytest.mark.asyncio
    async def test_close_unknown_session_is_false(self):
        assert await self._manager().close("nope") is False

    @pytest.mark.asyncio
    async def test_close_all(self):
        manager = self._manager()
        manager.get("a")
        manager.get("b")
        assert await manager.close_all() == 2
        assert manager.session_ids() == []

    def test_statuses_cover_every_session(self):
        manager = self._manager()
        manager.get("a")
        manager.get("b")
        assert {s.session_id for s in manager.statuses()} == {"a", "b"}


# --------------------------- HTTP routes ---------------------------


class TestRoutes:
    """The surface the Reading Engine integrates against."""

    @pytest.fixture
    def client(self):
        return TestClient(app)

    @pytest.fixture(autouse=True)
    def _fake_engine(self, monkeypatch):
        """Give the routes a fresh registry backed by fake providers.

        Replacing the whole manager (not one engine) keeps each test isolated:
        the real registry is process-wide and would otherwise carry playback
        state between tests.
        """
        import backend.app.modules.audio_engine.router as router_module

        def factory(session_id: str) -> PlaybackEngine:
            engine = PlaybackEngine(
                provider=FakeSpeechProvider(),
                sink=NullAudioSink(),
                auto_advance=True,
                session_id=session_id,
            )
            engine.set_profile(FAST)
            return engine

        manager = AudioSessionManager(engine_factory=factory)
        monkeypatch.setattr(router_module, "session_manager", manager)
        return manager

    def test_start_returns_state_and_pointer(self, client):
        response = client.post(
            "/audio/start",
            json={
                "pointer": {"page_index": 3, "paragraph_index": 1, "sentence_index": 0},
                "text": THREE_SENTENCES,
                "profile": "adaptive",
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["data"]["pointer"]["page_index"] == 3
        assert body["data"]["profile_name"] == "adaptive"

    def test_unknown_profile_falls_back(self, client):
        response = client.post(
            "/audio/start", json={"text": "Hello.", "profile": "nonexistent"}
        )
        assert response.json()["data"]["profile_name"] == "normal"

    def test_pause_resume_cycle(self, client):
        client.post("/audio/start", json={"text": THREE_SENTENCES})
        pause = client.post("/audio/pause", json={"reason": "meaning_mode"})
        assert pause.status_code == 200
        resume = client.post("/audio/resume")
        assert resume.status_code == 200

    def test_seek_updates_pointer(self, client):
        client.post("/audio/start", json={"text": THREE_SENTENCES})
        response = client.post(
            "/audio/seek", json={"pointer": {"page_index": 1, "sentence_index": 2}}
        )
        assert response.status_code == 200

    def test_stop_returns_idle(self, client):
        client.post("/audio/start", json={"text": THREE_SENTENCES})
        response = client.post("/audio/stop")
        assert response.json()["data"]["state"] == "idle"

    def test_status_shape(self, client):
        response = client.get("/audio/status")
        assert response.status_code == 200
        data = response.json()["data"]
        assert "state" in data
        assert "elapsed_reading_ms" in data

    def test_profiles_listed(self, client):
        response = client.get("/audio/profiles")
        names = {p["name"] for p in response.json()["data"]["profiles"]}
        assert {"normal", "adaptive", "disability"} <= names

    def test_voices_from_fake_provider(self, client):
        response = client.get("/audio/voices")
        assert response.status_code == 200
        assert response.json()["data"]["provider"] == "fake"

    def test_refresh_queue_route(self, client):
        client.post("/audio/start", json={"text": THREE_SENTENCES})
        response = client.post("/audio/refresh-queue", json={"text": "Updated text here."})
        assert response.status_code == 200

    def test_invalid_pointer_rejected(self, client):
        """page_index must be >= 1."""
        response = client.post(
            "/audio/start", json={"pointer": {"page_index": 0}, "text": "Hi."}
        )
        assert response.status_code == 422

    def test_start_requires_text(self, client):
        assert client.post("/audio/start", json={}).status_code == 422

    def test_routes_isolate_sessions(self, client):
        """Two readers over HTTP must not share playback state."""
        client.post(
            "/audio/start",
            params={"session_id": "alice"},
            json={"pointer": {"page_index": 4}, "text": THREE_SENTENCES},
        )
        client.post(
            "/audio/start",
            params={"session_id": "bob"},
            json={"pointer": {"page_index": 9}, "text": THREE_SENTENCES},
        )
        client.post("/audio/pause", params={"session_id": "alice"}, json={"reason": "meaning_mode"})

        alice = client.get("/audio/status", params={"session_id": "alice"}).json()["data"]
        bob = client.get("/audio/status", params={"session_id": "bob"}).json()["data"]

        assert alice["state"] == "paused"
        assert bob["state"] != "paused"
        assert alice["pointer"]["page_index"] == 4
        assert bob["pointer"]["page_index"] == 9

    def test_sessions_route_lists_active(self, client):
        client.post("/audio/start", params={"session_id": "alice"}, json={"text": "Hi."})
        client.post("/audio/start", params={"session_id": "bob"}, json={"text": "Hi."})

        body = client.get("/audio/sessions").json()["data"]
        assert body["count"] == 2
        assert {s["session_id"] for s in body["sessions"]} == {"alice", "bob"}

    def test_delete_session_removes_it(self, client):
        client.post("/audio/start", params={"session_id": "alice"}, json={"text": "Hi."})
        response = client.delete("/audio/session", params={"session_id": "alice"})

        assert response.json()["data"]["applied"] is True
        assert client.get("/audio/sessions").json()["data"]["count"] == 0

    def test_delete_unknown_session_reports_not_applied(self, client):
        response = client.delete("/audio/session", params={"session_id": "ghost"})
        assert response.json()["data"]["applied"] is False
        # Must not resurrect the engine it just failed to find.
        assert client.get("/audio/sessions").json()["data"]["count"] == 0

    def test_stale_refresh_reported_over_http(self, client):
        client.post("/audio/start", json={"text": THREE_SENTENCES})
        client.post("/audio/refresh-queue", json={"text": "Newer.", "source_version": 4})
        response = client.post(
            "/audio/refresh-queue", json={"text": "Older.", "source_version": 2}
        )

        body = response.json()
        assert body["data"]["applied"] is False
        assert "stale" in body["message"].lower()

    def test_status_exposes_statistics_and_version(self, client):
        client.post("/audio/start", json={"text": THREE_SENTENCES})
        data = client.get("/audio/status").json()["data"]

        assert "statistics" in data
        assert data["queue_version"] >= 1
        assert data["session_id"] == "default"


class TestRefreshAfterQueueRanDry:
    """A refresh that arrives after the queue emptied must still be spoken.

    Found by `scripts/stress_session.py`. `refresh_queue` filled the queue but
    only the *seek* path knew how to restart a finished playback loop, so
    sentences OCR delivered late sat queued with nothing to speak them: the
    reader heard the paragraph stop partway because OCR was still improving it.
    Silent — no error, no state anyone would look at twice.
    """

    @pytest.mark.asyncio
    async def test_late_text_is_spoken_rather_than_stranded(self):
        engine = build_engine()
        await engine.start(pointer=ReadingPointer(page_index=1), text="One two. Three four.")
        await engine.wait_for_idle()
        assert engine.get_status().state is PlaybackState.FINISHED
        spoken_before = engine.get_status().statistics.sentences_spoken

        # OCR reveals that the paragraph was longer than the first frame showed.
        applied = await engine.refresh_queue(
            text="One two. Three four. Five six. Seven eight.", source_version=99
        )
        await engine.wait_for_idle()

        status = engine.get_status()
        assert applied
        assert status.statistics.sentences_spoken > spoken_before
        assert status.queued_sentences == 0, "sentences left queued will never be spoken"

    @pytest.mark.asyncio
    async def test_a_paused_reader_is_not_restarted_by_a_refresh(self):
        """The other half of the fix. Reviving on PAUSED would resume narration
        under a reader who put the book down — their pause, their resume."""

        engine = build_engine()
        await engine.start(pointer=ReadingPointer(page_index=1), text=THREE_SENTENCES)
        await engine.pause()

        await engine.refresh_queue(text=THREE_SENTENCES + " And another.", source_version=99)
        await asyncio.sleep(0.05)

        assert engine.get_status().state is PlaybackState.PAUSED


class TestRefreshKeepsTheSentenceTheReaderIsOn:
    """A refresh with nothing being spoken must not drop the anchor sentence.

    Found by `backend/app/simulated_session.py` on a real page: the reading
    engine starts playback before Merge Memory has any text, so every sentence
    the reader hears arrives by refresh — with nothing in flight and the anchor
    sitting on a sentence that has not been spoken. `replace_after` is exclusive,
    so each refresh dropped exactly that sentence, and an OCR paragraph that
    segments to one sentence was dropped whole. A session narrated nothing while
    reporting 55 successful queue refreshes.
    """

    @pytest.mark.asyncio
    async def test_a_refresh_before_anything_is_spoken_keeps_the_first_sentence(self):
        engine = build_engine()
        # What `ReadingEngine.start_session` does: playback starts before Merge
        # Memory has produced any text for the page.
        await engine.start(pointer=ReadingPointer(page_index=1), text="")
        assert engine.get_status().state is PlaybackState.FINISHED

        await engine.refresh_queue(text=THREE_SENTENCES)
        await engine.wait_for_idle()

        status = engine.get_status()
        assert engine._provider.spoken[0] == "First sentence."
        assert status.statistics.sentences_spoken == 3
        assert status.queued_sentences == 0

    @pytest.mark.asyncio
    async def test_a_single_sentence_paragraph_is_not_dropped_whole(self):
        """The real-page case: one refresh, one sentence, nothing spoken."""

        engine = build_engine()
        await engine.start(pointer=ReadingPointer(page_index=1), text="")
        await engine.refresh_queue(text="A whole paragraph on one line.")
        await engine.wait_for_idle()

        assert engine._provider.spoken == ["A whole paragraph on one line."]

    @pytest.mark.asyncio
    async def test_the_sentence_being_spoken_is_still_never_re_spoken(self):
        """The other side of the same branch, which is why it is a branch.

        With a sentence genuinely in flight the anchor is already out of the
        queue, and including it would make the reader hear it twice. Needs
        `BlockingProvider`: with the fake one the loop has finished the sentence
        and cleared `_current` by the time a test can look.
        """

        provider = BlockingProvider()
        engine = build_engine(provider=provider)
        await engine.start(pointer=ReadingPointer(), text=THREE_SENTENCES, profile=FAST)
        await provider.wait_until_speaking()

        await engine.refresh_queue(text=THREE_SENTENCES)
        provider.release()
        await engine.wait_for_idle()

        assert provider.spoken.count("First sentence.") == 1
