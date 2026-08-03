"""The one OCR pipeline: frame in, Merge Memory updated.

Exposes the four operations the rest of the system is allowed to ask for:

    process_frame()     a camera frame becomes recognised words
    detect_new_page()   are these words the same page we were on?
    merge_page()        fold new words into the page we hold
    update_memory()     all three of the above, in order, once

`update_memory` is the entry point the runtime actually calls; the other three
are separated because each one fails differently and the E2E scenarios need to
provoke them individually. A test that wants to prove a stale frame is rejected
should not have to stand up a camera to do it.

Nothing here knows which provider ran. `process_frame` takes whatever the camera
handed over and asks the injected provider to read it — swap Google Vision for
the JSON replay and this file does not change, which is the property that lets
the same pipeline serve a live session and a recorded scenario.

Preprocessing (CLAHE + sharpen) is applied before recognition because the
standalone processor did it and it measurably helped on ESP32 frames, which are
dim and low-contrast. It is skipped when OpenCV is absent rather than failing:
a slightly worse read beats no read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from backend.app.modules.ocr.models import RecognizedWord
from backend.app.modules.ocr.providers import (
    OcrProviderError,
    get_provider,
)

try:  # pragma: no cover - absence is the branch under test
    import cv2
    import numpy as np
except ImportError:  # pragma: no cover
    cv2 = None  # type: ignore[assignment]
    np = None  # type: ignore[assignment]


# How much of the previous page's text must reappear in a new frame for it to
# count as the same page. Chosen low because a camera nudge can drop half a page
# out of view; a frame sharing a third of its words with what we hold is far more
# likely to be a shifted view than a genuine page turn.
SAME_PAGE_OVERLAP_RATIO = 0.30

# Below this many recognised words a frame is treated as noise rather than as a
# page. A hand covering the book, a motion blur, or the reader's lap all produce
# a handful of spurious words, and letting them through would commit the real
# page to history and reset the pointer.
MIN_WORDS_FOR_A_PAGE = 5

# Paragraphs are split on a blank line, matching how Merge Memory expects them
# and how the demo's book file is written. Must match `_PARAGRAPH_BREAK` in
# `merge_memory/engine.py` exactly.
_PARAGRAPH_BREAK = "\n\n"

# Punctuation Vision reports as its own word box. Joined to the preceding word
# rather than left floating in a space: "realize , greatly" is what a plain
# space-join produces, and it reaches sentence segmentation, the OLED and TTS
# looking like that. A set, not a string — `in` on a string is a substring test,
# which would match "" and mis-handle a two-character token.
_TRAILING_PUNCTUATION = frozenset(".,;:!?)]}%")


def _join_words(words: list[str]) -> str:
    """Join one paragraph's words, attaching trailing punctuation.

    The standalone got this for free by using Vision's `fullTextAnnotation.text`.
    That string cannot be used here — it carries no paragraph structure at all
    (Vision emits single newlines at every printed line end and never a blank
    line), and structure is what Merge Memory rebuilds the page's shape from. So
    the text is built from the word hierarchy, which has the structure, and this
    restores the one thing the flat string did better.
    """

    result: list[str] = []
    for word in words:
        if result and word in _TRAILING_PUNCTUATION:
            result[-1] += word
        else:
            result.append(word)

    return " ".join(result)


@dataclass(frozen=True)
class FrameResult:
    """What one frame turned out to be.

    `page_changed` is reported rather than acted on. The pipeline does not move
    the reading pointer or commit a page on its own — the runtime decides what a
    page turn means, because Reading Speed wants to close an observation and the
    Audio Engine wants to rebuild its queue, and OCR should not be choosing the
    order those happen in.
    """

    words: tuple[RecognizedWord, ...] = ()
    text: str = ""
    version: int = 0
    page_changed: bool = False
    accepted: bool = False
    reason: str = ""
    provider: str = ""

    @property
    def word_count(self) -> int:
        return len(self.words)


@dataclass
class OcrPipeline:
    """Frames in, merged page text out, versioned.

    Holds the current page's words and nothing else. Page history belongs to
    Merge Memory, which this drives — duplicating it here is what would create
    the second source of truth the architecture forbids.
    """

    provider: Any = None
    preprocess: bool = True
    # Injected so a scenario can drive merging deterministically, and so the
    # Groq-backed reconstruction in `merge_memory` can be swapped for the
    # geometric merge without this file knowing which one ran.
    merge_text: Callable[[str, str], str] | None = None

    _words: tuple[RecognizedWord, ...] = field(default_factory=tuple, init=False)
    _version: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if self.provider is None:
            self.provider = get_provider()

    @property
    def version(self) -> int:
        """Monotonic version of the page currently held.

        Only ever increases, including across page changes: consumers compare
        versions to reject stale frames, and a version that reset on a page turn
        would make the first frame of page 2 look older than the last frame of
        page 1.
        """

        return self._version

    @property
    def words(self) -> tuple[RecognizedWord, ...]:
        return self._words

    @property
    def text(self) -> str:
        """The held page as text, with paragraph breaks preserved.

        Grouped by `paragraph_index` rather than joined with spaces throughout.
        The provider goes to the trouble of reporting paragraph structure and
        Merge Memory splits on a blank line to rebuild it; joining every word
        with a space threw that away in between, so a 40-paragraph page arrived
        as one 281-word run. Sentence segmentation, the reading pointer and the
        audio queue are all downstream of that shape.

        Words within a paragraph keep the provider's order, which is the reading
        order it resolved from the geometry.
        """

        if not self._words:
            return ""

        paragraphs: list[list[str]] = []
        current: int | None = None
        for word in self._words:
            index = getattr(word, "paragraph_index", None)
            if index != current or not paragraphs:
                paragraphs.append([])
                current = index
            paragraphs[-1].append(word.text)

        return _PARAGRAPH_BREAK.join(_join_words(group) for group in paragraphs if group)

    def process_frame(self, source: Any) -> tuple[RecognizedWord, ...]:
        """Recognise the words in one frame. No state is changed.

        Raises `OcrProviderError` on transport or credential failure so the
        caller can retry, and returns empty for a frame that genuinely contained
        no text. Those two are not the same event.
        """

        if self.preprocess:
            source = self._enhance(source)
        if not self.provider.accepts(source):
            raise OcrProviderError(
                f"{self.provider.provider_name} cannot read a "
                f"{type(source).__name__} source"
            )
        return tuple(self.provider.extract(source))

    def detect_new_page(self, words: tuple[RecognizedWord, ...]) -> bool:
        """Whether `words` came from a different page than the one held.

        Compares vocabulary overlap rather than asking a language model. The
        standalone version sent both texts to Groq for a YES/NO, which cost a
        network round trip on every single frame and defaulted to "same page" on
        any error — so an outage silently disabled page detection. Set overlap is
        free, deterministic, and testable; the LLM is kept for reconstructing
        text, where it earns its latency.
        """

        if not self._words or not words:
            return False
        if len(words) < MIN_WORDS_FOR_A_PAGE:
            return False

        held = {word.text.lower() for word in self._words}
        incoming = {word.text.lower() for word in words}
        if not incoming:
            return False

        overlap = len(held & incoming) / len(incoming)
        return overlap < SAME_PAGE_OVERLAP_RATIO

    def merge_page(self, words: tuple[RecognizedWord, ...]) -> tuple[RecognizedWord, ...]:
        """Fold newly recognised words into the page currently held.

        Keeps the higher-confidence reading of any word both frames saw, and
        adds words only this frame saw. A later frame is not automatically
        better: the reader's hand moves, and half a page read clearly should not
        be replaced by the same half read through a shadow.

        Ordering follows the incoming frame where it overlaps, because that is
        the geometry actually observed most recently.
        """

        if not self._words:
            return words

        best: dict[str, RecognizedWord] = {}
        for word in self._words:
            best[self._key(word)] = word
        for word in words:
            key = self._key(word)
            existing = best.get(key)
            if existing is None or word.confidence > existing.confidence:
                best[key] = word

        # Preserve the incoming frame's reading order, then append anything it
        # did not see, so text that scrolled out of view is not lost.
        ordered: list[RecognizedWord] = []
        seen: set[str] = set()
        for word in words:
            key = self._key(word)
            ordered.append(best[key])
            seen.add(key)
        for word in self._words:
            key = self._key(word)
            if key not in seen:
                ordered.append(best[key])
                seen.add(key)
        return tuple(ordered)

    def update_memory(self, source: Any) -> FrameResult:
        """The whole pipeline for one frame, in the order it must happen.

        Recognise, decide whether the page changed, merge (or replace), bump the
        version. Returns what happened rather than raising for an ignored frame,
        because a blurred frame is routine and the caller's correct response is
        to keep going.
        """

        try:
            words = self.process_frame(source)
        except OcrProviderError as error:
            return FrameResult(
                words=self._words,
                text=self.text,
                version=self._version,
                accepted=False,
                reason=str(error),
                provider=getattr(self.provider, "provider_name", ""),
            )

        provider_name = getattr(self.provider, "provider_name", "")

        if not words:
            return FrameResult(
                words=self._words,
                text=self.text,
                version=self._version,
                accepted=False,
                reason="no text detected in frame",
                provider=provider_name,
            )

        page_changed = self.detect_new_page(words)
        if page_changed:
            # A new page replaces rather than merges. Merging across a turn is
            # what produces text that reads as two pages interleaved.
            self._words = words
            reason = "page turn detected"
        else:
            merged = self.merge_page(words)
            if len(merged) == len(self._words) and self._words:
                reason = "frame added nothing new"
            else:
                reason = f"merged to {len(merged)} words"
            self._words = merged

        self._version += 1
        return FrameResult(
            words=self._words,
            text=self.text,
            version=self._version,
            page_changed=page_changed,
            accepted=True,
            reason=reason,
            provider=provider_name,
        )

    def reset(self) -> None:
        """Drop the held page, keeping the version.

        Called when the runtime commits a page. The version survives on purpose:
        see `version`.
        """

        self._words = ()

    @staticmethod
    def _key(word: RecognizedWord) -> str:
        """Identity of a word for merge purposes: text plus roughly where it sat.

        Position is quantised to 20px because the same word in two frames never
        lands on the same pixel — the camera moves. Fine enough to keep two
        occurrences of "the" on different lines apart, coarse enough that a small
        nudge does not read as a new word.
        """

        return f"{word.text.lower()}@{int(word.center_x // 20)},{int(word.center_y // 20)}"

    def _enhance(self, source: Any) -> Any:
        """CLAHE + sharpen, for the dim low-contrast frames an ESP32 sends.

        Only applied to raw bytes and arrays. A path or URL is left alone: the
        provider will fetch it, and decoding it here purely to re-encode it would
        cost quality for nothing.
        """

        if cv2 is None or np is None:
            return source
        if not isinstance(source, (bytes, bytearray)):
            return source

        try:
            image = cv2.imdecode(np.frombuffer(source, np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                return source
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            enhanced = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
            sharpened = cv2.filter2D(
                enhanced, -1, np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]])
            )
            success, encoded = cv2.imencode(".jpg", sharpened)
            return encoded.tobytes() if success else source
        except cv2.error:
            # A frame that will not decode is the provider's problem to report,
            # not a reason to abort the session here.
            return source
