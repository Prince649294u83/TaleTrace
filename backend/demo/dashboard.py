"""The live dashboard.

One screen, repainted in place, showing every module at once. Watching expected
and actual progress move side by side is what makes a wrong number obvious — a
deviation figure on its own is impossible to sanity-check while a run is in
flight.

Rendering only. It reads state and prints it; it never asks any module to do
anything. Every value shown is fetched fresh at paint time rather than cached, so
the screen cannot show something the system no longer believes.

Reuses the glyph and encoding handling from `console.py` rather than repeating it:
Windows terminals still default to cp1252, and a UnicodeEncodeError halfway
through a demo is a silly way to lose the point.
"""

import os
import shutil
import sys
from typing import Any

from backend.app.modules.audio_engine.models import PlaybackStatus
from backend.app.modules.reading_speed.models import (
    DifficultyMetrics,
    ProgressSnapshot,
    ReadingBaseline,
    ReadingPrediction,
)
from backend.app.shared import ReadingSessionState
from backend.demo.console import G, _safe

# Repainting in place needs the frame to be a fixed height, or the cursor-up
# count drifts and the screen tears. Events get a fixed-size window and older
# ones scroll out of it.
EVENT_ROWS = 6


class Dashboard:
    """A repainting one-screen view of a live session.

    Falls back to plain scrolling output where ANSI is unavailable — a redraw that
    emits raw escape codes into a log file is worse than no redraw.
    """

    def __init__(self, *, width: int | None = None, ansi: bool | None = None) -> None:
        self.width = width or min(shutil.get_terminal_size((80, 24)).columns, 78)
        self._ansi = _supports_ansi() if ansi is None else ansi
        self._painted = 0
        self._events: list[str] = []

    def event(self, label: str, detail: str = "") -> None:
        """Record an event for the log pane. Does not repaint on its own."""

        entry = f"{label}" + (f"  {detail}" if detail else "")
        self._events.append(entry)
        if len(self._events) > 50:
            self._events = self._events[-EVENT_ROWS * 2 :]

    def draw(
        self,
        *,
        state: ReadingSessionState,
        snapshot: ProgressSnapshot,
        prediction: ReadingPrediction,
        baseline: ReadingBaseline,
        observed_wpm: float,
        audio: PlaybackStatus | None = None,
        difficulties: list[DifficultyMetrics] | None = None,
        book_title: str = "",
        chapter: str = "",
        page_count: int = 0,
        interactive: bool = False,
        # Everything below is optional so a caller that only has a reading
        # session still gets a screen. The demo predates the full runtime, and a
        # dashboard that raises when a module is absent is useless for exactly
        # the degraded runs it exists to explain.
        sentence: str = "",
        memory_version: int | None = None,
        ocr: Any = None,
        gesture: Any = None,
        explanation: Any = None,
        review: Any = None,
        session_seconds: float | None = None,
    ) -> None:
        lines: list[str] = []
        w = self.width

        lines.append("=" * w)
        lines.append("TALETRACE LIVE SIMULATION".center(w))
        lines.append("=" * w)

        where = f"page {snapshot.pointer.page_index}"
        if page_count:
            where += f" of {page_count}"
        lines.append(self._row("Book", f"{book_title}   {where}"))
        if chapter:
            lines.append(self._row("Chapter", chapter))
        lines.append(
            self._row(
                "Pointer",
                f"p{snapshot.pointer.page_index} "
                f"{G['para']}{snapshot.pointer.paragraph_index} "
                f"s{snapshot.pointer.sentence_index}",
            )
        )

        # The sentence the pointer names, spelled out. Every other row is a
        # number that can look right while pointing at the wrong text; this is
        # the one that says whether the pointer is where the reader is.
        if sentence:
            lines.append(self._row("Sentence", f"{G['arrow']} {_clip(sentence, w - 22)}"))

        mode = snapshot.mode.value.upper()
        clock = "running" if snapshot.is_clock_running else "FROZEN"
        lines.append(self._row("Mode", f"{mode:<12} reading clock {clock}"))

        # Shown even when nothing is wrong, because the failure it describes is
        # silent: with the camera off the pointer stops moving while the clock
        # runs on, and the deviation below grows exactly as it would for a reader
        # who had slowed to a crawl. Without this row those two look identical.
        lines.append(
            self._row(
                "Camera",
                "streaming" if state.camera_active else "OFF - gesture and OCR blind",
            )
        )

        # The input half of the chain: what the two camera consumers last made of
        # a frame, and which version of the page everything downstream is reading.
        # A stalled version with a healthy OCR row means frames are arriving and
        # adding nothing — a different problem from frames not arriving.
        lines.append(self._row("OCR", _ocr_status(ocr)))
        lines.append(self._row("Gesture", _gesture_status(gesture)))

        version = state.content_version if memory_version is None else memory_version
        lines.append(self._row("Merge Memory", f"v{version}"))

        lines.append("-" * w)

        # Pace. The baseline is labelled with its origin because deviation measured
        # against a guess is not a finding, and the screen should not present the
        # two identically.
        origin = baseline.method.value
        if not baseline.is_evidence:
            origin += " (not evidence)"
        lines.append(self._row("Baseline", f"{baseline.baseline_wpm:>3.0f} wpm   {origin}"))
        lines.append(self._row("Observed pace", f"{observed_wpm:>3.0f} wpm"))

        # Expected against actual, side by side. A single deviation number cannot
        # be checked by eye; two numbers and their difference can.
        lines.append(
            self._row(
                "Progress",
                f"expected {prediction.expected_progress_percentage:>5.1f}%"
                f"    actual {prediction.progress_percentage:>5.1f}%",
            )
        )
        lines.append(
            self._row(
                "Words",
                f"expected {prediction.expected_word_offset:>5}"
                f"    actual {prediction.actual_word_offset:>5}",
            )
        )

        if prediction.deviation_words == 0:
            drift = "on pace"
        else:
            direction = "ahead" if prediction.is_ahead else "behind"
            drift = f"{abs(prediction.deviation_words)} words {direction}"
        lines.append(
            self._row("Deviation", f"{drift:<20} confidence {prediction.confidence:.2f}")
        )

        lines.append(
            self._row(
                "Est. finish",
                _ms(prediction.expected_finish_ms)
                + (f"   this page {_ms(prediction.expected_page_finish_ms)}"
                   if prediction.expected_page_finish_ms is not None else ""),
            )
        )

        lines.append("-" * w)

        lines.append(
            self._row(
                "Friction",
                f"{snapshot.lookup_count} lookups   "
                f"{snapshot.meaning_mode_count} meaning   "
                f"{snapshot.pointer_corrections} corrections",
            )
        )
        lines.append(
            self._row(
                "Clocks",
                f"reading {_ms(snapshot.elapsed_reading_ms)}   "
                f"wall {_ms(snapshot.elapsed_wall_ms)}"
                + (
                    f"   session {_ms(int(session_seconds * 1000))}"
                    if session_seconds is not None else ""
                ),
            )
        )

        # Difficulty exists only for pages the reader has finished. The current page
        # deliberately shows nothing: its time is still accumulating while its words
        # have not been credited, so any verdict now would read as difficult and then
        # correct itself at the turn. Showing "pending" is the honest state.
        if difficulties:
            last = difficulties[-1]
            verdict = f"page {last.page_index}: {last.difficulty.value.upper()}"
            if last.evidence:
                verdict += f"  ({last.evidence[0]})"
            lines.append(self._row("Difficulty", verdict))
        else:
            lines.append(self._row("Difficulty", "pending - no page finished yet"))

        # TTS. Shown as OFF rather than hidden when silent: whether the narration
        # set the pace or the reader did changes what every number above means.
        if audio is not None and state.tts_enabled:
            speaking = audio.current_sentence or ""
            if len(speaking) > w - 26:
                speaking = speaking[: w - 29] + "..."
            reason = f" ({audio.pause_reason.value})" if audio.pause_reason else ""
            lines.append(
                self._row(
                    "TTS",
                    f"{audio.state.value}{reason:<12} {audio.provider or ''}"
                    f"  {audio.voice_id or ''}",
                )
            )
            lines.append(
                self._row("Voice", f"{audio.profile_name or '-'} profile"
                          f"   queue {audio.queued_sentences} v{audio.queue_version}")
            )
            if speaking:
                lines.append(self._row("", f"{G['arrow']} {speaking}"))
        else:
            lines.append(self._row("TTS", "off - silent reading"))
            lines.append(self._row("Voice", "-"))

        # The AI rows. Split into the last lookup and the end-of-session review
        # because they fail independently and mean different things: an
        # explanation that failed cost the reader one word, a review that failed
        # cost them the whole session's flashcards and quiz.
        lines.append(self._row("AI", _ai_status(explanation)))
        if explanation is not None and getattr(explanation, "ok", False):
            oled = str(getattr(explanation, "oled_text", ""))
            if oled:
                lines.append(self._row("", f"{G['arrow']} {_clip(oled, w - 22)}"))

        lines.append(self._row("Flashcards", _count_status(review, "flashcards")))
        lines.append(self._row("Quiz", _count_status(review, "quiz")))
        lines.append(self._row("Summary", _summary_status(review)))

        lines.append("=" * w)

        for entry in self._events[-EVENT_ROWS:]:
            lines.append(f"  {_clip(_safe(entry), w - 4)}")
        for _ in range(EVENT_ROWS - min(len(self._events), EVENT_ROWS)):
            lines.append("")

        if interactive:
            lines.append("-" * w)
            lines.append("  pause  resume  meaning  lookup  gesture N  page  summary  quit")

        self._paint(lines)

    def stop(self) -> None:
        """Leave the final frame on screen and stop repainting over it."""

        self._painted = 0
        sys.stdout.write("\n")
        sys.stdout.flush()

    # -------------------------------------------------------------- internals

    def _row(self, label: str, value: str) -> str:
        return _clip(f"  {label:<14}{_safe(value)}", self.width)

    def _paint(self, lines: list[str]) -> None:
        if self._ansi and self._painted:
            # Return to the top of the previous frame and clear each line as it is
            # rewritten; without the clear, a shorter line leaves the tail of the
            # longer one that was under it.
            sys.stdout.write(f"\x1b[{self._painted}A")
            body = "\n".join(f"\x1b[2K{line}" for line in lines)
        else:
            body = "\n".join(lines)

        sys.stdout.write(body + "\n")
        sys.stdout.flush()
        self._painted = len(lines)


def _supports_ansi() -> bool:
    """Whether repainting in place will work here.

    A redraw that emits escape codes into a pipe or a CI log produces noise, so
    anything that is not an interactive terminal gets the scrolling fallback.
    """

    if not sys.stdout.isatty():
        return False
    if os.name != "nt":
        return True
    # Windows Terminal, VS Code and modern conhost handle VT; legacy cmd does not.
    return bool(os.environ.get("WT_SESSION") or os.environ.get("TERM_PROGRAM"))


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1]


# The status helpers below all distinguish three states, not two: never ran,
# ran and failed, ran and produced something. A subsystem that was never called
# looks identical to one that returned nothing if you only print its result, and
# those two need different responses — one is a wiring problem, the other is a
# bad frame.


def _ocr_status(result: Any) -> str:
    """Whether the last frame produced text, and whether it was used."""

    if result is None:
        return "idle - no frame yet"

    words = len(getattr(result, "words", ()) or ())
    provider = getattr(result, "provider", "") or "?"

    if not getattr(result, "accepted", False):
        reason = getattr(result, "reason", "") or "rejected"
        return f"REJECTED  {reason}   ({provider})"

    turned = "  PAGE TURN" if getattr(result, "page_changed", False) else ""
    return f"ok  {words} words  v{getattr(result, 'version', 0)}   ({provider}){turned}"


def _gesture_status(result: Any) -> str:
    """What the reader last pointed at, or why nothing was selected.

    The failure cases are kept apart because only one of them is the reader's
    problem: no finger means they are not pointing, low confidence means they
    are and the system declined to guess.
    """

    if result is None:
        return "idle - no selection yet"

    status = getattr(getattr(result, "status", None), "value", "unknown")
    if not getattr(result, "succeeded", False):
        return f"{status}"

    word = getattr(result, "selected_word", "") or "?"
    confidence = float(getattr(result, "confidence", 0.0))
    line = getattr(result, "line_index", -1)
    return f"'{word}'  line {line}  confidence {confidence:.2f}"


def _ai_status(outcome: Any) -> str:
    """The last AI call. A failure is shown, not hidden — the session continued."""

    if outcome is None:
        return "idle - no request yet"
    if getattr(outcome, "ok", False):
        return f"ok  {getattr(outcome, 'capability', 'ai')}"
    return f"FAILED  {getattr(outcome, 'error', 'unknown error')}"


def _count_status(review: Any, key: str) -> str:
    """How many of `key` the review produced. Part of the same one call."""

    if review is None:
        return "pending - session not finished"
    if not getattr(review, "ok", False):
        return f"unavailable  ({getattr(review, 'error', 'AI call failed')})"

    items = getattr(review, "data", {}).get(key) or []
    return f"{len(items)} generated" if items else "none generated"


def _summary_status(review: Any) -> str:
    if review is None:
        return "pending - session not finished"
    if not getattr(review, "ok", False):
        return f"unavailable  ({getattr(review, 'error', 'AI call failed')})"

    text = str(getattr(review, "data", {}).get("session_summary", "")).strip()
    return f"ready  {text}" if text else "ready  (empty)"


def _ms(value: int | None) -> str:
    if value is None:
        return "--"
    seconds = value / 1000.0
    if abs(seconds) < 60:
        return f"{seconds:.1f}s"
    return f"{int(seconds) // 60}m{int(abs(seconds)) % 60:02d}s"
