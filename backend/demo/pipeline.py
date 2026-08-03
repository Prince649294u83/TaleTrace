"""The unified simulation pipeline — one event source, many consumers.

This is where the ownership rules become executable rather than aspirational:

    Fake Book -> Fake Merge Memory -> SimulatedReadingEngine -> ReadingSessionState
                                                |
                                    +-----------+-----------+
                                    |                       |
                              Audio Engine           Reading Speed
                              (plays speech)      (predicts, analyses)

`SimulatedReadingEngine` owns the session state and the pointer. Nothing else
writes to them. Audio Engine and Reading Speed both *read* the same state and are
told about the same events, which is the whole point: if the two consumers ever
disagree about where the reader is, the bug is visible here rather than in
production.

The real Reading Engine will replace this class. Everything downstream of it —
the service calls, the event order, the state shape — stays the same.
"""

import logging

from backend.app.modules.audio_engine.models import (
    PauseReason,
    PlaybackStatistics,
    ReadingPointer,
)
from backend.app.modules.audio_engine.playback_engine import PlaybackEngine
from backend.app.modules.audio_engine.sentence_queue import segment_sentences
from backend.app.modules.reading_speed.models import ContentMap, SentenceSpan
from backend.app.modules.reading_speed.service import ReadingSpeedService
from backend.app.shared import ReadingSessionState, SessionEvent
from backend.demo.fake_merge_memory import FakeMergeMemory

logger = logging.getLogger(__name__)


def content_map_from_memory(memory: FakeMergeMemory, *, version: int = 1) -> ContentMap:
    """Derive a Reading Speed ContentMap from Merge Memory.

    Both consumers must describe the same book or the simulation proves nothing:
    the audio engine speaks Merge Memory's paragraphs while Reading Speed counts
    words from a map built elsewhere, and any disagreement between them looks like
    a prediction bug rather than two different books.

    So the map is built from the same paragraphs the audio engine will speak, using
    the same segmenter. Reading Speed still never sees the text — only the counts
    and pointers this produces.
    """

    spans: list[SentenceSpan] = []
    page_words: dict[int, int] = {}

    for page in memory.pages:
        total = 0
        for paragraph_index, text in enumerate(page.paragraphs):
            for chunk in segment_sentences(
                text,
                start_pointer=ReadingPointer(
                    page_index=page.page_index,
                    paragraph_index=paragraph_index,
                    sentence_index=0,
                ),
            ):
                words = len(chunk.text.split())
                spans.append(
                    SentenceSpan(
                        pointer=chunk.pointer,
                        word_count=words,
                        character_count=len(chunk.text),
                    )
                )
                total += words
        page_words[page.page_index] = total

    return ContentMap(
        sentences=tuple(spans), page_word_counts=page_words, source_version=version
    )


class SimulatedReadingEngine:
    """Owns the reading session. Every other module reads from it.

    Stands in for the real Reading Engine. Its job is not to be clever — it is to
    be the *only* writer of session state, so that the two consumers can never
    drift apart. Each method below is one event, and each one does the same three
    things in the same order:

        1. update the state it owns
        2. tell Reading Speed
        3. tell the Audio Engine

    Reading Speed is told first throughout. It only ever records, so a failure
    there cannot leave audio half-started; the reverse is not true.
    """

    def __init__(
        self,
        *,
        session_id: str,
        reader_id: str,
        memory: FakeMergeMemory,
        speed: ReadingSpeedService,
        audio: PlaybackEngine | None = None,
    ) -> None:
        self._memory = memory
        self._speed = speed
        self._audio = audio

        self._content = content_map_from_memory(memory)
        self._state = ReadingSessionState(
            session_id=session_id,
            reader_id=reader_id,
            book_id="demo-lighthouse",
            pointer=ReadingPointer(page_index=1, paragraph_index=0, sentence_index=0),
            content_version=self._content.source_version,
            is_reading=False,
        )
        self._events: list[tuple[SessionEvent, str]] = []

    # ------------------------------------------------------------------ reads

    @property
    def state(self) -> ReadingSessionState:
        """The session state. Read-only to everyone but this class."""

        return self._state

    @property
    def content(self) -> ContentMap:
        return self._content

    @property
    def session_id(self) -> str:
        return self._state.session_id

    @property
    def events(self) -> list[tuple[SessionEvent, str]]:
        return list(self._events)

    def current_text(self) -> str:
        """Merge Memory's current best text for the paragraph being read."""

        return self._memory.paragraph(
            self._state.pointer.page_index, self._state.pointer.paragraph_index
        )

    # ----------------------------------------------------------------- events

    async def start_session(self, *, profile=None, voice_id: str | None = None) -> None:
        """SESSION_STARTED. Both consumers begin from the same pointer."""

        self._state = self._state.model_copy(
            update={"is_reading": True, "is_paused": False, "is_meaning_mode": False}
        )
        self._record(SessionEvent.SESSION_STARTED, f"page {self._state.pointer.page_index}")

        self._speed.start_session(
            session_id=self.session_id,
            reader_id=self._state.reader_id,
            pointer=self._state.pointer,
            content=self._content,
        )

        if self._audio is not None:
            await self._audio.start(
                pointer=self._state.pointer,
                text=self.current_text(),
                profile=profile,
                voice_id=voice_id,
            )

    async def pause(self) -> None:
        """SESSION_PAUSED. The reader set the book down — says nothing about the text."""

        self._state = self._state.model_copy(update={"is_paused": True})
        self._record(SessionEvent.SESSION_PAUSED, "")
        self._speed.pause(self.session_id)
        if self._audio is not None:
            await self._audio.pause(reason=PauseReason.USER)

    async def resume(self) -> None:
        """SESSION_RESUMED."""

        self._state = self._state.model_copy(
            update={"is_paused": False, "is_meaning_mode": False}
        )
        self._record(SessionEvent.SESSION_RESUMED, "")
        self._speed.resume(self.session_id)
        if self._audio is not None:
            await self._audio.resume()

    async def meaning_mode_on(self) -> None:
        """MEANING_MODE_ON. A pause that *is* a statement about the text.

        Counted apart from an ordinary pause because the two mean opposite things:
        a user pause says nothing about the page, while entering Meaning Mode says
        the reader hit something they could not read past.
        """

        self._state = self._state.model_copy(update={"is_meaning_mode": True})
        self._record(SessionEvent.MEANING_MODE_ON, "word tapped")
        self._speed.meaning_mode(self.session_id, active=True)
        if self._audio is not None:
            await self._audio.pause(reason=PauseReason.MEANING_MODE)

    async def meaning_mode_off(self) -> None:
        """MEANING_MODE_OFF. Resumes the interrupted sentence, not the next one."""

        self._state = self._state.model_copy(update={"is_meaning_mode": False})
        self._record(SessionEvent.MEANING_MODE_OFF, "")
        self._speed.meaning_mode(self.session_id, active=False)
        if self._audio is not None:
            await self._audio.resume()

    def lookup_completed(self, word: str = "") -> None:
        """LOOKUP_COMPLETED. Friction evidence — the AI Engine reports this.

        Not async and not sent to audio: a completed lookup changes nothing about
        playback, it only tells Reading Speed that the reader needed help here.
        """

        self._record(SessionEvent.LOOKUP_COMPLETED, word)
        self._speed.lookup_completed(self.session_id)

    async def move_pointer(self, pointer: ReadingPointer, *, corrected: bool = False) -> None:
        """READING_POINTER_UPDATED. The one write path for the pointer.

        Gesture calls this; it does not touch the pointer itself. `corrected=True`
        marks the reader dragging the pointer back because it was wrong, which is
        friction; ordinary forward movement is not.
        """

        previous_page = self._state.pointer.page_index
        self._state = self._state.model_copy(
            update={
                "pointer": pointer,
                "page_index": pointer.page_index,
                "paragraph_index": pointer.paragraph_index,
                "sentence_index": pointer.sentence_index,
            }
        )

        if pointer.page_index != previous_page:
            self._record(SessionEvent.PAGE_CHANGED, f"{previous_page} -> {pointer.page_index}")
            self._memory.turn_to(pointer.page_index)
        else:
            self._record(
                SessionEvent.READING_POINTER_UPDATED,
                f"s{pointer.sentence_index}" + (" (correction)" if corrected else ""),
            )

        # Reading Speed infers the page change from the pointer, so Gesture only
        # ever has to report a position.
        self._speed.update_pointer(self.session_id, pointer, corrected=corrected)

        if self._audio is not None:
            if pointer.page_index != previous_page:
                # A new page is new text from sentence zero: start() rebuilds the
                # queue rather than splicing into the old one.
                await self._audio.start(pointer=pointer, text=self.current_text())
            else:
                await self._audio.seek(pointer=pointer, text=self.current_text())

    async def refresh_content(self, *, stale: bool = False) -> bool:
        """CONTENT_UPDATED. A new OCR frame refined the page.

        Returns whether it was applied. A stale frame being rejected is normal:
        OCR updates arrive over HTTP and can overtake each other.
        """

        if stale:
            version = 1
            accepted = self._speed.update_content(
                self.session_id,
                content_map_from_memory(self._memory, version=version),
            )
        else:
            version = self._memory.refine()
            content = content_map_from_memory(self._memory, version=version)
            accepted = self._speed.update_content(self.session_id, content)
            if accepted:
                self._content = content
                self._state = self._state.model_copy(update={"content_version": version})

        self._record(
            SessionEvent.CONTENT_UPDATED,
            f"v{version} {'applied' if accepted else 'REJECTED as stale'}",
        )

        if self._audio is not None:
            await self._audio.refresh_queue(
                text=self.current_text(), source_version=version
            )
        return accepted

    async def finish_session(self, *, ingest_tts: bool = False):
        """SESSION_FINISHED. Stops both clocks and returns the analysis.

        When TTS was on, the audio engine's statistics are handed to Reading Speed
        rather than letting it infer word counts from pointer movement — the engine
        counted them while speaking, which is strictly more accurate.
        """

        self._state = self._state.model_copy(
            update={"is_reading": False, "is_paused": False, "is_meaning_mode": False}
        )
        self._record(SessionEvent.SESSION_FINISHED, "")

        playback: PlaybackStatistics | None = None
        if self._audio is not None:
            await self._audio.stop()
            if ingest_tts:
                playback = self._audio.final_statistics

        return self._speed.finish_session(self.session_id, playback=playback)

    # -------------------------------------------------------------- internals

    def _record(self, event: SessionEvent, detail: str) -> None:
        self._events.append((event, detail))
        logger.info(
            "[reading_engine:%s] %s%s",
            self.session_id,
            event.value,
            f" {detail}" if detail else "",
        )
