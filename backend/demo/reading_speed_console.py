"""Live console for the Reading Speed rehearsal.

Rendering only. It reads snapshots and predictions and prints them; it never asks
the service to do anything. Kept separate from the timeline so the sequence of
events stays readable as a sequence of events.

Reuses the glyph and cp1252 handling from `console.py` rather than repeating it —
one console with two views, not two consoles.
"""

from backend.demo.console import G, WIDTH, _safe, banner, rule
from backend.app.modules.reading_speed.models import (
    DifficultyLevel,
    ProgressSnapshot,
    ReadingBaseline,
    ReadingPrediction,
    SessionAnalytics,
)


def _ms(value: int | None) -> str:
    """Milliseconds as something a person can read at a glance."""

    if value is None:
        return "--"
    seconds = value / 1000.0
    if abs(seconds) < 60:
        return f"{seconds:.1f}s"
    return f"{int(seconds) // 60}m{int(abs(seconds)) % 60:02d}s"


def baseline_panel(baseline: ReadingBaseline) -> None:
    banner("READER BASELINE")
    print(f"  {'Baseline':<20} {baseline.baseline_wpm:.0f} wpm")
    print(f"  {'Established by':<20} {baseline.method.value}")
    # Spelled out because it decides what the rest of the run may claim:
    # deviation measured against a guess is not a finding.
    print(f"  {'Usable as evidence':<20} {'yes' if baseline.is_evidence else 'no'}")
    rule()


def header() -> None:
    print()
    print(
        f"  {'TIME':>7} {'MODE':<9} {'PAGE':>4} {'EXPECT':>7} {'ACTUAL':>7} "
        f"{'DEVIATION':>10} {'FINISH':>7} {'CONF':>5}"
    )
    print(f"  {'-' * (WIDTH - 2)}")


def tick(
    snapshot: ProgressSnapshot,
    prediction: ReadingPrediction,
    *,
    observed_wpm: float,
) -> None:
    """One line per sampled second.

    Shows expected against actual word position side by side, because a single
    'deviation' number is impossible to sanity-check while a run is in flight.
    """

    if prediction.deviation_words == 0:
        drift = "on pace"
    else:
        direction = "ahead" if prediction.is_ahead else "behind"
        drift = f"{abs(prediction.deviation_words):>3}w {direction}"

    print(
        f"  {_ms(snapshot.elapsed_reading_ms):>7} "
        f"{snapshot.mode.value:<9} "
        f"{snapshot.pointer.page_index:>4} "
        f"{prediction.expected_word_offset:>7} "
        f"{prediction.actual_word_offset:>7} "
        f"{drift:>10} "
        f"{_ms(prediction.expected_finish_ms):>7} "
        f"{prediction.confidence:>5.2f}"
    )


def mode_line(snapshot: ProgressSnapshot, *, tts: bool) -> None:
    """The reader's current state, including what is frozen.

    Meaning Mode and TTS are shown together because they are the two things that
    change what the numbers above mean: one stops the reading clock, the other
    means the narration set the pace rather than the reader.
    """

    frozen = "" if snapshot.is_clock_running else f"  {G['bullet']} reading clock frozen"
    print(
        f"      mode={snapshot.mode.value}  tts={'on' if tts else 'off'}  "
        f"lookups={snapshot.lookup_count}  meaning={snapshot.meaning_mode_count}"
        f"{frozen}"
    )


def event(label: str, detail: str = "") -> None:
    """An event reported *to* Reading Speed by something else.

    Printed distinctly from the sampled lines to keep the causality visible: the
    module never generates these, it only receives them.
    """

    print()
    print(f"  >>> {_safe(label)}")
    if detail:
        print(f"      {_safe(detail)}")


_DIFFICULTY_MARK = {
    DifficultyLevel.LOW: "low",
    DifficultyLevel.MEDIUM: "MEDIUM",
    DifficultyLevel.HIGH: "HIGH",
    DifficultyLevel.UNKNOWN: "unknown",
}


def analytics_panel(analytics: SessionAnalytics) -> None:
    print()
    banner("SESSION ANALYTICS")
    print(f"  {'Baseline':<22} {analytics.baseline_wpm:.0f} wpm")
    print(f"  {'Session pace':<22} {analytics.session_wpm:.0f} wpm")
    print(f"  {'Pace vs baseline':<22} {analytics.pace_vs_baseline:.2f}x")
    print(f"  {'Words read':<22} {analytics.words_read}")
    print(f"  {'Pages read':<22} {analytics.pages_read}")
    print(f"  {'Reading time':<22} {_ms(analytics.reading_duration_ms)}")
    print(f"  {'Wall time':<22} {_ms(analytics.wall_duration_ms)}")
    print(f"  {'Lookups':<22} {analytics.lookup_count}")
    print(f"  {'Meaning requests':<22} {analytics.meaning_requests}")
    print(f"  {'TTS assisted':<22} {'yes' if analytics.tts_assisted else 'no'}")
    print(f"  {'Hardest page':<22} {analytics.hardest_page or '--'}")
    print(f"  {'Easiest page':<22} {analytics.easiest_page or '--'}")
    print(
        f"  {'Suggested baseline':<22} "
        f"{f'{analytics.suggested_baseline_wpm:.0f} wpm' if analytics.suggested_baseline_wpm else 'not enough evidence'}"
    )
    rule()

    print()
    print("  PER-PAGE DIFFICULTY")
    print(f"  {'-' * (WIDTH - 2)}")
    for page in analytics.pages:
        print(
            f"  page {page.page_index}  {_DIFFICULTY_MARK[page.difficulty]:<8} "
            f"{page.words:>4}w  expected {_ms(page.expected_ms):>6}  "
            f"actual {_ms(page.actual_ms):>6}  ({page.deviation_ratio:+.0%})"
        )
        # The evidence is printed with the verdict on purpose: a slow page with no
        # lookups is an interruption, not a difficulty, and the reader of this
        # output should be able to see which one they are looking at.
        for reason in page.evidence:
            print(f"           {G['bullet']} {_safe(reason)}")
    rule()
