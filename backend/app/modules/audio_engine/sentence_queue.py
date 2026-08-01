"""Sentence buffer between Merge Memory and playback.

The Merge Engine keeps improving the page text as more OCR frames arrive. If
playback read from Merge Memory directly, every improvement would restart the
sentence being spoken. The queue absorbs that: the current sentence is already
dequeued and in flight, so a refresh only ever rewrites what has not been
spoken yet.
"""

import re
from collections import deque

from backend.app.modules.audio_engine.models import ReadingPointer, SentenceChunk

# Split after . ! ? — optionally followed by a closing quote or bracket — but
# only where the next sentence actually begins: an opening quote or bracket
# followed by a capital or digit. The lookahead is what keeps dialogue intact.
# In '"Stop!" she cried.' the lowercase 'she' shows the sentence is still
# running, so it is not split at the '!'. Dialogue is everywhere in the novels
# TaleTrace targets, so this case matters.
#
# The two fixed-width lookbehinds are deliberate: only whitespace is consumed as
# the separator, so a closing quote stays attached to the sentence it ends.
# Folding the quote into the separator instead would silently drop it from
# exchanges like '"Run!" "Why?"'.
#
# Deliberately a regex rather than nltk: nltk needs a runtime corpus download,
# which is a poor thing to depend on mid-demo.
_SENTENCE_BOUNDARY = re.compile(
    r'(?:(?<=[.!?])|(?<=[.!?]["\'”’)\]]))\s+(?=["\'“‘(\[]*[A-Z0-9])'
)

# A terminating '.' that is really an abbreviation, not a sentence end. Without
# this, 'Dr. Smith arrived.' is spoken as two sentences with a pause after
# 'Dr.', which is plainly wrong to the ear.
_ABBREVIATIONS = frozenset(
    {
        "mr", "mrs", "ms", "dr", "prof", "st", "sr", "jr", "vs", "etc",
        "fig", "vol", "ch", "pp", "no", "approx", "dept", "est", "inc",
    }
)

_TRAILING_WORD = re.compile(r'([A-Za-z]+)\.["\'”’)\]]*$')

# Rough speaking pace for duration estimates.
_WORDS_PER_MINUTE = 150.0


def _ends_with_abbreviation(text: str) -> bool:
    """True when `text` ends in an abbreviation rather than a real terminator.

    A single trailing letter counts too, so initials like 'J. K. Rowling' stay
    in one piece.
    """

    match = _TRAILING_WORD.search(text)
    if not match:
        return False
    word = match.group(1).lower()
    return word in _ABBREVIATIONS or len(word) == 1


def _split_sentences(text: str) -> list[str]:
    """Split into sentences, rejoining splits that fell after an abbreviation."""

    pieces = [piece.strip() for piece in _SENTENCE_BOUNDARY.split(text)]
    merged: list[str] = []

    for piece in (p for p in pieces if p):
        if merged and _ends_with_abbreviation(merged[-1]):
            merged[-1] = f"{merged[-1]} {piece}"
        else:
            merged.append(piece)

    return merged


def estimate_duration_ms(text: str, rate: float = 1.0) -> int:
    """Approximate spoken length, used only for status reporting."""

    words = len(text.split())
    if not words or rate <= 0:
        return 0
    minutes = words / (_WORDS_PER_MINUTE * rate)
    return int(minutes * 60_000)


def segment_sentences(
    text: str,
    *,
    start_pointer: ReadingPointer | None = None,
    rate: float = 1.0,
) -> list[SentenceChunk]:
    """Split a paragraph into chunks, numbering each from `start_pointer`.

    Sentence indices continue from the supplied pointer so a mid-paragraph
    start stays addressable.
    """

    base = start_pointer or ReadingPointer()
    cleaned = (text or "").strip()
    if not cleaned:
        return []

    pieces = _split_sentences(cleaned)
    chunks: list[SentenceChunk] = []

    for offset, piece in enumerate(pieces):
        pointer = base.model_copy(
            update={
                "sentence_index": base.sentence_index + offset,
                "character_offset": 0,
            }
        )
        chunks.append(
            SentenceChunk(
                text=piece,
                pointer=pointer,
                duration_estimate_ms=estimate_duration_ms(piece, rate),
            )
        )

    return chunks


class SentenceQueue:
    """Implements SentenceQueueInterface — FIFO of pending sentences.

    Carries a `version` that increments on every mutation. Playback reads it to
    tell "the queue I am speaking from" apart from "the queue Merge Memory has
    since replaced", which is what makes a refresh observable rather than a
    silent swap.
    """

    def __init__(self) -> None:
        self._items: deque[SentenceChunk] = deque()
        self._version = 0

    @property
    def version(self) -> int:
        """Bumped on every change to the pending sentences."""

        return self._version

    def _bump(self) -> None:
        self._version += 1

    def enqueue(self, chunk: SentenceChunk) -> None:
        self._items.append(chunk)
        self._bump()

    def extend(self, chunks: list[SentenceChunk]) -> None:
        if not chunks:
            return
        self._items.extend(chunks)
        self._bump()

    def dequeue(self) -> SentenceChunk | None:
        # Not a version bump: taking a sentence to speak is normal consumption,
        # not a rewrite of what is pending.
        return self._items.popleft() if self._items else None

    def peek(self) -> SentenceChunk | None:
        return self._items[0] if self._items else None

    def clear(self) -> None:
        if self._items:
            self._items.clear()
            self._bump()

    def replace(self, chunks: list[SentenceChunk]) -> None:
        """Swap the pending sentences wholesale.

        The in-flight sentence is not in the queue, so it is untouched.
        """

        self._items = deque(chunks)
        self._bump()

    def replace_after(self, pointer: ReadingPointer, chunks: list[SentenceChunk]) -> None:
        """Requeue only the sentences that follow `pointer`.

        Used on a Merge refresh: everything at or before the sentence being
        spoken is preserved, and the tail is rewritten from the updated text.
        """

        anchor = pointer.sentence_order_key()
        self._items = deque(
            chunk for chunk in chunks if chunk.pointer.sentence_order_key() > anchor
        )
        self._bump()

    def drop_before(self, pointer: ReadingPointer) -> int:
        """Discard queued sentences that precede `pointer`.

        Used by a Reading Update that moves the pointer without supplying new
        text: whatever is queued stays, minus what the reader has already passed.
        Returns how many were dropped, which the engine reports as skipped.
        """

        anchor = pointer.sentence_order_key()
        kept = deque(
            chunk
            for chunk in self._items
            if chunk.pointer.sentence_order_key() >= anchor
        )
        dropped = len(self._items) - len(kept)
        if dropped:
            self._items = kept
            self._bump()
        return dropped

    def push_front(self, chunk: SentenceChunk) -> None:
        """Return a chunk to the head of the queue.

        Pausing mid-sentence requeues it so resume re-speaks that sentence from
        its start instead of skipping ahead — nothing the reader half-heard is lost.
        """

        self._items.appendleft(chunk)
        self._bump()

    def size(self) -> int:
        return len(self._items)

    def __bool__(self) -> bool:
        return bool(self._items)
