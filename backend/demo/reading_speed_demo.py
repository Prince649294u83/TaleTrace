"""Full Reading Speed rehearsal.

Runs a complete session — calibration, on-pace reading, Meaning Mode, lookups, a
gesture correction, a long unexplained pause, a stale OCR map, session analytics
— against the real service, with the unfinished modules replaced by fakes.

    python -m backend.demo.reading_speed_demo
    python -m backend.demo.reading_speed_demo --realtime
    python -m backend.demo.reading_speed_demo --tts

Default is compressed time, so the whole session runs instantly and works as a
smoke test. `--realtime` runs it at human speed, which is the version to watch if
you want to see the deviation column drift while a page is being read.

What this is for that unit tests are not: the numbers have to be *plausible
together*. A pace, a deviation, and a difficulty verdict can each be individually
correct and still contradict one another, and that only shows up when you watch
them side by side across a whole session.

Exits non-zero if any expected behaviour is missing, so it is usable in CI.
"""

import argparse
import logging

from backend.app.modules.audio_engine.models import PlaybackStatistics
from backend.app.modules.reading_speed.models import DifficultyLevel
from backend.demo import console
from backend.demo import reading_speed_console as view
from backend.demo.fake_events import (
    READER_ID,
    SESSION_ID,
    Timeline,
    compressed_timeline,
    realtime_timeline,
)
from backend.demo.fake_reader import ScriptedReader, build_content

# The calibration passage: 180 words in 60 seconds is 180 wpm. Deliberately the
# example from the design, so the number on screen is checkable by hand.
CALIBRATION_WORDS = 180
CALIBRATION_MS = 60_000


def _fake_playback_statistics(analytics_words: int, reading_ms: int) -> PlaybackStatistics:
    """What the audio engine would hand over after narrating this session.

    Deliberately *not* equal to the tracker's own numbers: the engine counts words
    as it speaks them, which is a different quantity from words the reader
    covered. The demo passes them so `--tts` shows narration reported alongside
    reading progress without redefining it — `words_read` must stay at the
    tracker's count, and only `tts_assisted` should change.
    """

    return PlaybackStatistics(
        sentences_spoken=27,
        words_spoken=analytics_words - 12,
        pages_read=3,
        meaning_mode_count=1,
        reading_time_ms=reading_ms,
        playback_time_ms=reading_ms + 26_000,
    )


def _check(analytics, baseline, service) -> list[tuple[bool, str, str]]:
    """Assert the behaviours this rehearsal exists to demonstrate.

    Every entry maps to a scripted step. A check that would pass in a broken state
    is worse than no check, so these assert *specific* outcomes — a difficulty
    verdict, a rejected version, a frozen clock — not merely that numbers exist.
    """

    pages = {page.page_index: page for page in analytics.pages}
    page_two = pages.get(2)
    page_three = pages.get(3)

    return [
        (
            baseline.baseline_wpm == 180.0 and baseline.is_evidence,
            "calibration measured a usable baseline",
            f"{baseline.baseline_wpm:.0f} wpm from {CALIBRATION_WORDS} words",
        ),
        (
            analytics.words_read > 0 and analytics.session_wpm > 0,
            "session recorded words and a pace",
            f"{analytics.words_read} words at {analytics.session_wpm:.0f} wpm",
        ),
        (
            analytics.pages_read == 3,
            "every page was closed and observed",
            f"{analytics.pages_read} pages",
        ),
        (
            analytics.reading_duration_ms < analytics.wall_duration_ms,
            "reading clock excluded the pauses",
            f"read {analytics.reading_duration_ms / 1000:.0f}s of "
            f"{analytics.wall_duration_ms / 1000:.0f}s wall",
        ),
        (
            analytics.lookup_count >= 2,
            "lookups were recorded as friction",
            f"{analytics.lookup_count} lookup(s)",
        ),
        (
            analytics.meaning_requests >= 1,
            "meaning mode was counted apart from a pause",
            f"{analytics.meaning_requests} request(s)",
        ),
        (
            page_two is not None
            and page_two.difficulty in (DifficultyLevel.MEDIUM, DifficultyLevel.HIGH),
            "slow page WITH lookups was rated difficult",
            f"page 2 {page_two.difficulty.value}" if page_two else "page 2 never closed",
        ),
        (
            page_three is not None and page_three.difficulty is DifficultyLevel.UNKNOWN,
            "slow page WITHOUT lookups was not blamed on the text",
            f"page 3 {page_three.difficulty.value}"
            if page_three
            else "page 3 never closed",
        ),
        (
            all(page.evidence for page in analytics.pages),
            "every verdict carried its evidence",
            f"{sum(len(p.evidence) for p in analytics.pages)} reason(s) across "
            f"{len(analytics.pages)} page(s)",
        ),
        (
            service.tracker(SESSION_ID).content.source_version == 9,
            "stale merge memory was rejected",
            f"held v{service.tracker(SESSION_ID).content.source_version}, refused v4",
        ),
        (
            service.baseline_for(READER_ID).baseline_wpm == 180.0,
            "reading the summary did not move the baseline",
            f"still {service.baseline_for(READER_ID).baseline_wpm:.0f} wpm",
        ),
        (
            analytics.suggested_baseline_wpm is not None
            and analytics.suggested_baseline_wpm != 180.0,
            "a baseline update was suggested, not applied",
            f"suggested {analytics.suggested_baseline_wpm:.0f} wpm"
            if analytics.suggested_baseline_wpm
            else "no suggestion",
        ),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--realtime",
        action="store_true",
        help="Run at human speed instead of compressing time.",
    )
    parser.add_argument(
        "--tts",
        action="store_true",
        help="Ingest audio-engine statistics, as if the session were narrated.",
    )
    parser.add_argument("--verbose", action="store_true", help="Show module log lines.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(message)s",
    )

    console.banner(
        "READING SPEED REHEARSAL",
        "realtime" if args.realtime else "compressed time",
    )

    content = build_content(pages=3)
    reader = ScriptedReader(content=content, baseline_wpm=180.0)

    # Three pages, three deliberately different paces, so the deviation column has
    # something to say and the difficulty verdicts are predictable.
    reader.set_page_pace(1, 1.0)   # on pace
    reader.set_page_pace(2, 0.45)  # struggling, and it will look like it
    reader.set_page_pace(3, 0.55)  # also slow, but with no lookups to explain it

    if args.realtime:
        from backend.app.modules.reading_speed.service import ReadingSpeedService

        service = ReadingSpeedService()
        timeline: Timeline = realtime_timeline(service, reader)
    else:
        service, timeline = compressed_timeline(reader)

    baseline = service.calibrate(
        READER_ID, word_count=CALIBRATION_WORDS, elapsed_ms=CALIBRATION_MS
    )
    view.baseline_panel(baseline)

    timeline.run()

    playback = None
    if args.tts:
        snapshot = service.tracker(SESSION_ID).snapshot()
        playback = _fake_playback_statistics(
            snapshot.words_confirmed, snapshot.elapsed_reading_ms
        )

    analytics = service.finish_session(SESSION_ID, playback=playback)
    view.analytics_panel(analytics)

    return console.verdict(_check(analytics, baseline, service))


if __name__ == "__main__":
    raise SystemExit(main())
