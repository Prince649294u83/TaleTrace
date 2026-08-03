"""What Merge Memory promises its consumers.

Merge Memory owns the text. OCR feeds it frames; it accumulates them into the
best current guess at what a page says. Two modules read the result and they want
different things from it:

    Audio Engine    the text, to speak it
    Reading Speed   the shape of the text, to measure progress through it

This file is the contract for both, expressed as a Protocol rather than a class.
Structural typing means the real Merge Engine and the simulator's fake satisfy it
by having the right methods, with no import of this file and no base class to
inherit. That keeps the dependency pointing the correct way: consumers depend on
the contract, and the implementation is free not to know the contract exists.

Deliberately not defined here: a second content model. `ContentMap` already lives
in `reading_speed.models`, and restating it here would create two shapes that
must agree — which is the drift this layer exists to prevent. The Protocol
references the real one.

Versioning is the part every implementation must get right. Frames arrive over
HTTP and can overtake each other, so text is published with a monotonically
increasing version and consumers refuse anything older than what they hold. An
implementation that reuses or decrements a version will cause a consumer to
silently reject good text.
"""

from typing import Protocol, runtime_checkable

from backend.app.modules.reading_speed.models import ContentMap


@runtime_checkable
class MergeMemorySource(Protocol):
    """The read side of Merge Memory. Consumers only ever call these.

    No method here mutates anything a consumer can see. Merging is Merge Memory's
    own business, driven by OCR; a consumer that could trigger a merge would be
    able to change the text under another consumer mid-sentence.
    """

    @property
    def page_count(self) -> int:
        """Total pages currently known. Grows as OCR sees more of the book."""

    @property
    def version(self) -> int:
        """Merge version for the current page.

        Increases whenever a frame improves the page. Consumers store the version
        they last accepted and reject anything lower.
        """

    def paragraph(self, page_index: int, paragraph_index: int) -> str:
        """Best current text for one paragraph, or "" if there is none.

        May differ between calls: that is the point of merging. Returns the best
        guess available now, not a final answer, so a consumer that caches this
        string is caching something known to be provisional.
        """

    def paragraph_count(self, page_index: int) -> int:
        """How many paragraphs are on a page."""

    def content_map(self, *, source_version: int = 1) -> ContentMap:
        """The same text described as counts and pointers instead of strings.

        What Reading Speed consumes. Two requirements an implementation must meet:

        Sentence indices must match the ones the Audio Engine speaks — segment
        with the same segmenter, or a sentence index means two different things to
        two modules, and the reader appears to be running behind by exactly the
        offset between them.

        Word counts must come from clean text, not degraded OCR. A page whose
        early frames were misread does not contain fewer words, and counting the
        degraded version would make OCR quality look like reading speed.
        """
