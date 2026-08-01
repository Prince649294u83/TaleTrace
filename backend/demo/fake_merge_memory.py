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

_BOOK = Path(__file__).parent / "book.txt"


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

    @classmethod
    def from_book(cls, path: Path | None = None) -> "FakeMergeMemory":
        raw = (path or _BOOK).read_text(encoding="utf-8")
        blocks = [b.strip().replace("\n", " ") for b in raw.split("\n\n") if b.strip()]

        # Two paragraphs per page, so page turns happen often enough to watch.
        pages: list[Page] = []
        for i in range(0, len(blocks), 2):
            pages.append(Page(page_index=len(pages) + 1, paragraphs=blocks[i : i + 2]))
        return cls(pages=pages)

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
