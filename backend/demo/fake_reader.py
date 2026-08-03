"""A scripted reader, and a content map to read.

Stands in for the parts of TaleTrace that do not exist yet: OCR, Merge Memory,
the Reading Engine, Gesture. Everything here is deliberately dumb — it produces
pointers and word counts on a schedule and knows nothing about prediction.

The reader is scripted rather than random so a run is reproducible and the
verdict at the end can assert specific outcomes. A reader that drifts randomly
produces a demo that looks alive and proves nothing.
"""

from dataclasses import dataclass, field

from backend.app.modules.audio_engine.models import ReadingPointer
from backend.app.modules.reading_speed.models import ContentMap, SentenceSpan

# A page that looks like a page: sentences of uneven length, because uniform
# pages hide off-by-one errors in word accounting.
_PAGE_SHAPES: tuple[tuple[int, ...], ...] = (
    (14, 21, 9, 26, 17, 12, 23, 18),
    (19, 8, 31, 15, 11, 24, 16, 22, 13),
    (25, 17, 12, 29, 14, 20, 9, 18, 23, 11),
)


def build_content(pages: int = 3, *, source_version: int = 1) -> ContentMap:
    """A Merge Memory map: word counts and pointers, no text.

    Reading Speed reads content structure and never touches the text itself,
    which is why this carries counts and pointers but no strings.
    """

    spans: list[SentenceSpan] = []
    page_words: dict[int, int] = {}

    for page in range(1, pages + 1):
        shape = _PAGE_SHAPES[(page - 1) % len(_PAGE_SHAPES)]
        for index, words in enumerate(shape):
            spans.append(
                SentenceSpan(
                    pointer=ReadingPointer(
                        page_index=page, paragraph_index=0, sentence_index=index
                    ),
                    word_count=words,
                    character_count=words * 6,
                )
            )
        page_words[page] = sum(shape)

    return ContentMap(
        sentences=tuple(spans), page_word_counts=page_words, source_version=source_version
    )


@dataclass
class ScriptedReader:
    """Walks a content map at a chosen pace, reporting pointers as it goes.

    `pace_multiplier` is the whole point: 1.0 reads exactly at the baseline, 0.5
    is half speed. The simulator sets it per page so a run exercises on-pace, slow,
    and fast reading in one sitting — and the console can be checked against a
    deviation whose sign is known in advance.
    """

    content: ContentMap
    baseline_wpm: float
    pace_multiplier: float = 1.0
    index: int = 0
    words_read: int = 0
    _page_paces: dict[int, float] = field(default_factory=dict)

    def set_page_pace(self, page_index: int, multiplier: float) -> None:
        """Read one page faster or slower than the baseline."""

        self._page_paces[page_index] = multiplier

    @property
    def finished(self) -> bool:
        return self.index >= self.content.sentence_count

    @property
    def pointer(self) -> ReadingPointer:
        span = self.content.sentences[min(self.index, self.content.sentence_count - 1)]
        return span.pointer

    @property
    def page_index(self) -> int:
        return self.pointer.page_index

    def next_sentence_ms(self) -> int:
        """How long this reader will spend on the sentence they are about to read."""

        if self.finished:
            return 0
        span = self.content.sentences[self.index]
        multiplier = self._page_paces.get(span.pointer.page_index, self.pace_multiplier)
        words_per_ms = (self.baseline_wpm * multiplier) / 60_000.0
        return int(span.word_count / words_per_ms)

    def advance(self) -> ReadingPointer:
        """Finish the current sentence and move to the next.

        Returns the new pointer — which is all the Reading Engine would ever hand
        to Reading Speed.
        """

        if self.finished:
            return self.pointer
        self.words_read += self.content.sentences[self.index].word_count
        self.index += 1
        return self.pointer

    def jump_back(self, sentences: int = 2) -> ReadingPointer:
        """Re-read: the reader lost their place and dragged the pointer back."""

        self.index = max(0, self.index - sentences)
        return self.pointer
