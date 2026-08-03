"""Console rendering for the simulator.

Kept apart from the event timeline so the sequence of events stays readable as
a sequence of events, rather than being buried in formatting.
"""

import sys

from backend.app.modules.audio_engine.models import (
    PlaybackState,
    PlaybackStatistics,
    PlaybackStatus,
)
from backend.app.modules.audio_engine.sentence_queue import segment_sentences

WIDTH = 68

# Plain ASCII fallbacks: Windows consoles still default to cp1252, and a
# UnicodeEncodeError mid-demo would be a silly way to lose the point.
_FANCY = {"tick": "✓", "arrow": "►", "bullet": "•", "para": "¶", "to": "→"}
_PLAIN = {"tick": "*", "arrow": ">", "bullet": "-", "para": "P", "to": "->"}


def _glyphs() -> dict[str, str]:
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        "".join(_FANCY.values()).encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return _PLAIN
    return _FANCY


G = _glyphs()


# Typographic punctuation is legal cp1252 but arrives as mojibake anywhere the
# output is later read as UTF-8 (a pipe, a log file, CI). Outside UTF-8, fold it.
_PUNCTUATION = str.maketrans({"—": "-", "–": "-", "“": '"', "”": '"', "‘": "'", "’": "'", "…": "..."})


def safe(text: str) -> str:
    """Make text safe for the current console.

    Windows terminals still default to cp1252, and an em dash in a label is not
    worth a UnicodeEncodeError — or a stray replacement character — halfway
    through a demo.

    Public because text from outside the demo package needs it too: analytics
    evidence strings contain em dashes, and they are printed through here.
    """

    encoding = (getattr(sys.stdout, "encoding", None) or "ascii").lower()
    if encoding.replace("-", "") not in ("utf8", "utf16", "utf32"):
        text = text.translate(_PUNCTUATION)
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return text.encode("ascii", "replace").decode("ascii")
    return text


# The original name, kept because several demo modules import it.
_safe = safe


def rule(char: str = "=") -> None:
    print(char * WIDTH)


def banner(title: str, subtitle: str = "") -> None:
    rule()
    print(_safe(title).center(WIDTH))
    if subtitle:
        print(_safe(subtitle).center(WIDTH))
    rule()


def event(label: str, detail: str = "") -> None:
    """A simulated external event — what the Reading Engine would send."""

    print()
    print(f">>> {_safe(label)}")
    if detail:
        print(f"    {_safe(detail)}")


def field(label: str, value) -> None:
    print(f"  {label:<22} {_safe(str(value))}")


def speaking(text: str) -> None:
    print(f"  {G['arrow']} {_safe(text)}")


def timeline(status: PlaybackStatus, paragraph_text: str) -> None:
    """Render the paragraph with the current sentence marked.

    The engine does not keep spoken sentences (they are dequeued), so position
    is derived by re-segmenting the paragraph and comparing against the pointer.
    That is exactly how a debugging view should work — read-only, no state of
    its own to fall out of sync.
    """

    if not paragraph_text:
        return

    pointer = status.pointer
    chunks = segment_sentences(paragraph_text, start_pointer=None)
    current_index = pointer.sentence_index if pointer else -1

    print()
    rule("-")
    page = pointer.page_index if pointer else "?"
    paragraph = pointer.paragraph_index if pointer else "?"
    print(f"  Page {page}  |  Paragraph {paragraph}  |  queue v{status.queue_version}")
    rule("-")

    for i, chunk in enumerate(chunks):
        text = _safe(chunk.text if len(chunk.text) <= 54 else chunk.text[:51] + "...")
        if i < current_index:
            print(f"  {G['tick']} {text}")
        elif i == current_index:
            marker = "SPEAKING" if status.state is PlaybackState.PLAYING else status.state.value.upper()
            print(f"  {G['arrow']} {text}   {G['to']} {marker}")
        else:
            print(f"    {text}")
    rule("-")


def status_line(status: PlaybackStatus) -> None:
    state = status.state.value
    reason = f" ({status.pause_reason.value})" if status.pause_reason else ""
    pointer = status.pointer
    where = (
        f"p{pointer.page_index}/{G['para']}{pointer.paragraph_index}/s{pointer.sentence_index}"
        if pointer
        else "-"
    )
    print(
        f"    state={state}{reason}  at={where}  "
        f"queued={status.queued_sentences}  v={status.queue_version}"
    )


def summary(stats: PlaybackStatistics | None) -> None:
    print()
    banner("SESSION SUMMARY")
    if stats is None:
        print("  No statistics captured.")
        rule()
        return

    field("Sentences spoken", stats.sentences_spoken)
    field("Words spoken", stats.words_spoken)
    field("Characters spoken", stats.characters_spoken)
    field("Pages read", stats.pages_read)
    print()
    field("Pauses", stats.pause_count)
    field("Meaning Mode lookups", stats.meaning_mode_count)
    field("Reading updates", stats.reading_updates)
    field("Queue refreshes", stats.queue_refreshes)
    field("Stale frames rejected", stats.stale_updates_rejected)
    print()
    field("Reading time", f"{stats.reading_time_ms / 1000:.1f}s  (excludes pauses)")
    field("Playback time", f"{stats.playback_time_ms / 1000:.1f}s  (wall clock)")
    field("Average pace", f"{stats.average_wpm:.0f} wpm")
    rule()


def verdict(checks: list[tuple[bool, str, str]]) -> int:
    """Print the behaviour checklist and return a process exit code.

    The rehearsal is only useful if a broken run fails loudly, so this returns
    non-zero the moment any expected behaviour is missing — which makes the
    simulator usable as a smoke test, not just something to watch.
    """

    print()
    banner("BEHAVIOUR CHECKS")
    failed = 0
    for ok, label, detail in checks:
        # Spelled out rather than glyphed: a checklist where pass and fail look
        # similar is worse than no checklist.
        print(f"  {'PASS' if ok else 'FAIL'}  {_safe(label):<42} {_safe(detail)}")
        if not ok:
            failed += 1
    rule()

    if failed:
        print(f"\n  {failed} of {len(checks)} checks failed.")
        return 1

    print(f"\n  Session completed. All {len(checks)} checks passed.")
    return 0
