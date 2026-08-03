"""Fake Merge Memory — stands in for the OCR + Merge Engine pair.

The real Merge Engine accumulates OCR frames of the same physical page and keeps
improving its guess at the text. The audio engine only ever sees the *result*:
clean paragraph text, plus a version telling it how current that text is.

This fake reproduces that contract, including the part that matters most for
playback — early frames are wrong, and later frames correct them. Playback has
to survive text changing underneath it.
"""

from dataclasses import dataclass, field
from pathlib import Path

from backend.app.modules.audio_engine.models import ReadingPointer
from backend.app.modules.audio_engine.sentence_queue import segment_sentences
from backend.app.modules.reading_speed.models import ContentMap, SentenceSpan

_BOOK = Path(__file__).parent / "book.txt"

# Chapter titles by position in the file, since `book.txt` carries the breaks but
# not the names. Anything past the end falls back to "Chapter N", so adding a
# `---` to the book is enough to get a new chapter without editing this.
_CHAPTER_TITLES = {1: "Climbing", 2: "The Lamp"}


def _degrade(text: str, severity: int) -> str:
    """Approximate a low-confidence OCR read of `text`.

    Real OCR fails on letter shapes, not whole words, so the substitutions here
    are the usual confusions: rn/m, l/1, o/0. `severity` 0 is a clean read.
    """

    if severity <= 0:
        return text

    swaps = [("m", "rn"), ("l", "1"), ("o", "0"), ("e", "c")][:severity]
    out = text
    for original, broken in swaps:
        # Only the first occurrence per line, so the text stays recognisable.
        out = out.replace(original, broken, 1)
    return out


@dataclass
class Page:
    """One physical page as Merge Memory understands it."""

    page_index: int
    paragraphs: list[str]
    chapter: str = ""  # the chapter this page belongs to


@dataclass
class FakeMergeMemory:
    """Holds pages and serves progressively better text for the current one.

    `version` increments on every refinement. The audio engine uses it to reject
    a frame that arrives out of order — see PlaybackEngine.refresh_queue.
    """

    pages: list[Page] = field(default_factory=list)
    current_page_index: int = 1
    version: int = 0
    _refinements: int = 3
    chapters: dict[int, str] = field(default_factory=dict)  # page_index → title

    @classmethod
    def from_book(cls, path: Path | None = None) -> "FakeMergeMemory":
        raw = (path or _BOOK).read_text(encoding="utf-8")

        chapter_titles: dict[int, str] = {}
        pages: list[Page] = []

        # A `---` line divides chapters. Titles are positional rather than read
        # from the file: a real book states them, but OCR would deliver a chapter
        # heading as just another line of text, and inventing a parser for it
        # would be simulating a problem Merge Memory does not solve.
        sections = [s for s in raw.split("\n---") if s.strip()]
        for number, section in enumerate(sections, start=1):
            blocks = [
                block.strip().replace("\n", " ")
                for block in section.strip().split("\n\n")
                if block.strip()
            ]
            title = _CHAPTER_TITLES.get(number, f"Chapter {number}")

            # Two paragraphs per page, so page turns happen often enough to watch.
            # Pages never straddle a chapter break, which is why paging happens
            # inside this loop rather than over the flattened block list.
            for start in range(0, len(blocks), 2):
                index = len(pages) + 1
                chapter_titles[index] = title
                pages.append(
                    Page(
                        page_index=index,
                        paragraphs=blocks[start : start + 2],
                        chapter=title,
                    )
                )
        return cls(pages=pages, chapters=chapter_titles)

    def chapter(self, page_index: int) -> str:
        """The chapter title covering one page."""

        return self.chapters.get(page_index, "")

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def page(self, page_index: int) -> Page:
        return self.pages[page_index - 1]

    def paragraph(self, page_index: int, paragraph_index: int) -> str:
        """The current best text for one paragraph.

        Degraded while the page is still settling; clean once refined.
        """

        page = self.page(page_index)
        if paragraph_index >= len(page.paragraphs):
            return ""

        text = page.paragraphs[paragraph_index]
        severity = max(0, self._refinements - self.version)
        return _degrade(text, severity)

    def paragraph_count(self, page_index: int) -> int:
        return len(self.page(page_index).paragraphs)

    def refine(self) -> int:
        """Simulate another OCR frame improving the page. Returns the version."""

        self.version += 1
        return self.version

    def turn_to(self, page_index: int) -> int:
        """Move to a new physical page; its text starts unrefined again."""

        self.current_page_index = page_index
        self.version = 0
        return self.version

    def is_settled(self) -> bool:
        return self.version >= self._refinements

    # ---------- the structural view Reading Speed consumes ----------

    def content_map(self, *, source_version: int = 1) -> ContentMap:
        """The same book, described as counts and pointers instead of text.

        Reading Speed never sees strings — it needs to know how much text there is
        and where, not what it says. This builds that view from the *same* pages the
        Audio Engine is speaking, which is the reason it lives here rather than in a
        separate fake: two independent fakes drift, and then the deviation column is
        measuring one book against another.

        Sentences are segmented with the Audio Engine's own segmenter, so a sentence
        index means the same thing to both modules. Building this map with a
        different splitter would put the two modules a sentence out of step, which
        looks exactly like a reader running slightly behind.

        Word counts come from the clean text, not the degraded OCR text: a page whose
        early frames are misread does not contain fewer words, and counting the
        degraded version would make OCR quality look like reading speed.
        """

        from backend.app.modules.audio_engine.sentence_queue import segment_sentences
        from backend.app.modules.reading_speed.models import ContentMap, SentenceSpan

        spans: list[SentenceSpan] = []
        page_words: dict[int, int] = {}

        for page in self.pages:
            total = 0
            for paragraph_index, paragraph in enumerate(page.paragraphs):
                base = ReadingPointer(
                    page_index=page.page_index,
                    paragraph_index=paragraph_index,
                    sentence_index=0,
                )
                for chunk in segment_sentences(paragraph, start_pointer=base):
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
            sentences=tuple(spans),
            page_word_counts=page_words,
            source_version=source_version,
        )
