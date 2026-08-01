"""Fake Reading Engine — the audio engine's only real collaborator.

The Reading Engine owns the pointer and the session; the audio engine consumes
both. This fake implements exactly the surface the audio engine depends on and
nothing else, which is the point: if the simulator drives playback correctly
through this narrow surface, the real Reading Engine will only need to supply
the same data.

Deliberately absent: OCR, cameras, gestures, ESP32, the AI Engine.
"""

import logging

from backend.app.modules.audio_engine.models import PauseReason, ReadingPointer
from backend.app.modules.audio_engine.playback_engine import PlaybackEngine
from backend.demo.fake_merge_memory import FakeMergeMemory

logger = logging.getLogger(__name__)


class FakeReadingEngine:
    """Drives one PlaybackEngine from a FakeMergeMemory."""

    def __init__(self, *, engine: PlaybackEngine, memory: FakeMergeMemory) -> None:
        self._engine = engine
        self._memory = memory
        self._pointer = ReadingPointer(page_index=1, paragraph_index=0, sentence_index=0)

    @property
    def pointer(self) -> ReadingPointer:
        return self._pointer

    def current_text(self) -> str:
        """Merge Memory's current best text for the paragraph being read."""

        return self._memory.paragraph(self._pointer.page_index, self._pointer.paragraph_index)

    # ---------- the events the real Reading Engine will emit ----------

    async def start_session(self, *, profile=None, voice_id: str | None = None):
        """Camera ON: begin reading at the top of page one."""

        return await self._engine.start(
            pointer=self._pointer,
            text=self.current_text(),
            profile=profile,
            voice_id=voice_id,
        )

    async def move_pointer(self, *, sentence_index: int):
        """Reading Update: the reader's eyes moved within the paragraph.

        Text is passed along because the pointer and the text it indexes must
        stay consistent — a jump into a paragraph the engine has not been given
        would land on sentences it cannot see.
        """

        self._pointer = self._pointer.model_copy(
            update={"sentence_index": sentence_index, "character_offset": 0}
        )
        return await self._engine.seek(pointer=self._pointer, text=self.current_text())

    async def enable_meaning_mode(self):
        """The reader tapped a word they do not know. Stop talking immediately."""

        return await self._engine.pause(reason=PauseReason.MEANING_MODE)

    async def disable_meaning_mode(self):
        """Explanation delivered; pick up the interrupted sentence."""

        return await self._engine.resume()

    async def ocr_update(self):
        """A new OCR frame improved the page text.

        Passes Merge Memory's version so the engine can reject a frame that
        arrives after a newer one.
        """

        version = self._memory.refine()
        applied = await self._engine.refresh_queue(
            text=self.current_text(), source_version=version
        )
        return version, applied

    async def stale_ocr_update(self, *, version: int):
        """Deliver an out-of-order frame on purpose, to prove it is rejected."""

        return await self._engine.refresh_queue(
            text="This is stale text that must never be spoken.",
            source_version=version,
        )

    async def next_paragraph(self):
        """Advance within the same page."""

        nxt = self._pointer.paragraph_index + 1
        if nxt >= self._memory.paragraph_count(self._pointer.page_index):
            return None

        self._pointer = self._pointer.model_copy(
            update={"paragraph_index": nxt, "sentence_index": 0, "character_offset": 0}
        )
        return await self._engine.seek(pointer=self._pointer, text=self.current_text())

    async def turn_page(self):
        """Page Turn: commit the finished page and load the next one."""

        nxt = self._pointer.page_index + 1
        if nxt > self._memory.page_count:
            return None

        self._memory.turn_to(nxt)
        self._pointer = ReadingPointer(page_index=nxt, paragraph_index=0, sentence_index=0)
        # start(), not seek(): a new page is new text from sentence zero, and
        # start() rebuilds the queue rather than splicing into the old one.
        return await self._engine.start(pointer=self._pointer, text=self.current_text())

    async def end_session(self):
        """Camera OFF: stop playback and hand back the session statistics."""

        await self._engine.stop()
        return self._engine.final_statistics
