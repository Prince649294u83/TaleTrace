"""The single Merge Memory implementation: the book as the system currently knows it.

Migrated from `OCRandGESTURE/OCR_dynamicMem/taletrace_processor.py`, which held
the right idea — one active page accumulating OCR frames, committed to history on
a page turn — wrapped in things that belonged elsewhere. That file called Google
Vision directly, called Groq directly, printed its state to stdout, and owned a
reading pointer. All four are removed here:

    OCR          is upstream now; `apply_frame` receives text, it does not fetch it
    Groq         is optional and injected, so a merge never requires a network call
    printing     is the dashboard's job
    the pointer  belongs to the Reading Engine, which is its single writer

What remains is the part nothing else can do: hold the best current text, version
it, and describe its shape to Reading Speed.

Why this is not in `shared/`
----------------------------
The directive asked that Dynamic Memory become the implementation of
`shared/merge_memory.py`. That file is a `Protocol`, and deliberately so: it is
the contract *consumers* depend on. Putting an implementation there would make
`shared/` — the layer every module imports — depend on OCR, pydantic models and
optionally Groq, which inverts the direction the shared layer exists to enforce.
Reading Speed would then pull in an OCR provider just to read a word count.

So the contract stays in `shared/`, this is its one implementation, and
`MergeMemory` satisfies it structurally without importing it. There is still
exactly one Merge Memory in the system; only the dependency arrow differs, and it
now points inward like every other module's.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from backend.app.modules.audio_engine.sentence_queue import segment_sentences
from backend.app.modules.reading_speed.models import ContentMap, ReadingPointer, SentenceSpan
from backend.app.shared.constants import FIRST_PAGE_INDEX
from backend.app.shared.exceptions import StaleContentError

# A page is split into paragraphs on a blank line, matching how OCR reports
# paragraph structure and how the demo's book file is written.
_PARAGRAPH_BREAK = "\n\n"


@dataclass
class Page:
    """One page's accumulated text and how it got that way.

    `frames_merged` is kept because it is the honest measure of how much the
    system has actually seen of a page. A page built from one blurred frame and a
    page built from twelve clean ones are both "known", and a consumer deciding
    whether to trust the text needs to tell them apart.
    """

    page_index: int
    paragraphs: list[str] = field(default_factory=list)
    frames_merged: int = 0
    committed: bool = False

    @property
    def text(self) -> str:
        return _PARAGRAPH_BREAK.join(self.paragraphs)

    @property
    def word_count(self) -> int:
        return sum(len(paragraph.split()) for paragraph in self.paragraphs)


class MergeMemory:
    """The book as the system currently knows it. One instance per session.

    Satisfies `shared.merge_memory.MergeMemorySource` by structure. The read side
    (`paragraph`, `paragraph_count`, `content_map`, `version`, `page_count`) is
    what consumers touch; the write side (`apply_frame`, `commit_page`) is driven
    only by the OCR pipeline.
    """

    def __init__(
        self,
        *,
        reconstruct: Callable[[str, str], str] | None = None,
    ) -> None:
        """
        `reconstruct` is the optional text-repair step — in production the
        Groq-backed merge that rejoins sentences broken across frames. Left as
        `None` the memory still works by accumulating raw OCR text, so a session
        without an API key degrades to rougher text rather than to no text.
        """

        self._pages: dict[int, Page] = {}
        self._current_page = FIRST_PAGE_INDEX
        self._version = 0
        self._reconstruct = reconstruct

    # ------------------------------------------------------------------
    # Read side: the MergeMemorySource contract.
    # ------------------------------------------------------------------

    @property
    def page_count(self) -> int:
        return len(self._pages)

    @property
    def version(self) -> int:
        return self._version

    @property
    def current_page(self) -> int:
        return self._current_page

    def paragraph(self, page_index: int, paragraph_index: int) -> str:
        page = self._pages.get(page_index)
        if page is None or not 0 <= paragraph_index < len(page.paragraphs):
            return ""
        return page.paragraphs[paragraph_index]

    def paragraph_count(self, page_index: int) -> int:
        page = self._pages.get(page_index)
        return len(page.paragraphs) if page else 0

    def page_text(self, page_index: int) -> str:
        page = self._pages.get(page_index)
        return page.text if page else ""

    def content_map(self, *, source_version: int = 1) -> ContentMap:
        """Describe every known page as sentence spans and per-page word counts.

        Segmentation goes through the Audio Engine's `segment_sentences` rather
        than a local split. The contract requires that a sentence index mean the
        same thing to both modules; the only way to guarantee that is to use one
        segmenter, so this imports the one that already handles abbreviations.
        """

        spans: list[SentenceSpan] = []
        page_word_counts: dict[int, int] = {}

        for page_index in sorted(self._pages):
            page = self._pages[page_index]
            page_word_counts[page_index] = page.word_count

            for paragraph_index, paragraph in enumerate(page.paragraphs):
                chunks = segment_sentences(
                    paragraph,
                    start_pointer=ReadingPointer(
                        page_index=page_index,
                        paragraph_index=paragraph_index,
                        sentence_index=0,
                    ),
                )
                for chunk in chunks:
                    spans.append(
                        SentenceSpan(
                            pointer=chunk.pointer,
                            word_count=len(chunk.text.split()),
                            character_count=len(chunk.text),
                        )
                    )

        return ContentMap(
            sentences=tuple(spans),
            page_word_counts=page_word_counts,
            source_version=source_version,
        )

    # ------------------------------------------------------------------
    # Write side: driven by the OCR pipeline only.
    # ------------------------------------------------------------------

    def apply_frame(
        self,
        text: str,
        *,
        page_index: int | None = None,
        frame_version: int | None = None,
        whole_page: bool = False,
    ) -> int:
        """Merge one frame's text into a page and return the new version.

        `frame_version` lets a caller assert which version it believed it was
        improving. Frames arrive over HTTP and can overtake each other, so a
        frame built on a version older than the one held is rejected with
        `StaleContentError` rather than silently overwriting newer text.

        `whole_page` says which of two things `text` is, and getting it wrong is
        the one way to corrupt a page here. Two callers exist and they mean
        different things:

        *A fragment* (the default) is part of a page — the next few lines, or a
        sentence broken across frames. It is appended, because the rest of the
        page is text this frame did not contain.

        *A whole page* is `OcrPipeline.update_memory`'s output, which is already
        the merge of every frame of that page: the pipeline keeps the
        higher-confidence reading of each word and holds them in geometric order.
        Appending that would accumulate an already-accumulated page — the same
        words again on every frame, growing without bound while OCR's own count
        stayed still. So it replaces.

        The default is the fragment because that is the older contract and the
        one the tests were written against; the pipeline path opts in.

        `whole_page` chooses replace over append. It does **not** skip
        reconstruction: what the reconstructor does is repair raw OCR — drop the
        running header and the page number, close up words broken by a camera
        nudge, restore characters Vision lost — and a page that arrived whole
        needs every one of those as much as a fragment does. The reference called
        it on every frame including the first, which is why its prompt has an
        `[EMPTY - PAGE START]` case at all. Skipping it here put `hat's`, `ege`
        and a burned-in camera overlay into the text handed to the reader.
        """

        if frame_version is not None and frame_version < self._version:
            raise StaleContentError(self._version, frame_version)

        target = page_index if page_index is not None else self._current_page
        cleaned = (text or "").strip()
        if not cleaned:
            return self._version

        page = self._pages.get(target)
        if page is None:
            page = Page(page_index=target)
            self._pages[target] = page

        if self._reconstruct is not None:
            # `page.text` is empty on the first frame of a page, and the
            # reconstructor is built for that: it reads an empty memory as
            # "clean this OCR up", which is exactly the job here.
            held = "" if whole_page else page.text
            merged = self._reconstruct(held, cleaned)
            page.paragraphs = _to_paragraphs(merged or cleaned)
        elif whole_page or not page.paragraphs:
            # Nothing to merge with, and no reconstructor to ask.
            page.paragraphs = _to_paragraphs(cleaned)
        else:
            page.paragraphs = _to_paragraphs(f"{page.text}{_PARAGRAPH_BREAK}{cleaned}")

        page.frames_merged += 1
        self._current_page = target
        self._version += 1
        return self._version

    def begin_page(self, page_index: int) -> None:
        """Move the active page, committing the one being left.

        Called by the runtime when OCR reports a page turn. Committing here is
        what makes the previous page immutable, which is what lets the session
        summary describe a page that is no longer in view.
        """

        if page_index == self._current_page:
            return
        self.commit_page(self._current_page)
        self._current_page = page_index
        self._pages.setdefault(page_index, Page(page_index=page_index))

    def commit_page(self, page_index: int | None = None) -> bool:
        """Mark a page final. Returns whether anything was committed.

        A page with no text is not committed: an empty page in history would be
        counted by the summary as a page the reader read.
        """

        target = page_index if page_index is not None else self._current_page
        page = self._pages.get(target)
        if page is None or not page.paragraphs or page.committed:
            return False
        page.committed = True
        return True

    def committed_pages(self) -> list[Page]:
        """History, in reading order. What the session summary is built from."""

        return [self._pages[index] for index in sorted(self._pages) if self._pages[index].committed]

    @property
    def total_words(self) -> int:
        return sum(page.word_count for page in self._pages.values())


def _to_paragraphs(text: str) -> list[str]:
    """Split page text into paragraphs, dropping empties.

    OCR output arrives with inconsistent newlines — a single newline is usually a
    line wrap inside a paragraph, a blank line is a real break. Collapsing the
    former is what keeps a sentence from being split mid-clause.
    """

    blocks: list[str] = []
    for block in (text or "").split(_PARAGRAPH_BREAK):
        collapsed = " ".join(block.split())
        if collapsed:
            blocks.append(collapsed)
    return blocks
