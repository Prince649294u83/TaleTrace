"""Reading history, one point per calendar day.

This is the Analysis page's data source, and it *aggregates* — it does not
measure. Every number it touches was computed by the module that owns it and
written to a `sessions` row when the session ended: `session_wpm` by
`ReadingSpeedService`, `words_read` by the progress tracker, `difficulty` by
`analytics.assess_page`. Nothing here recomputes any of them from raw timings,
because a second implementation of a measurement is a second answer to it.

Two of those columns carry a sentinel, and averaging a sentinel is the whole
reason this file exists rather than a `GROUP BY` in the route:

- `session_wpm == 0.0` means *not measurable*, not "read at zero words a
  minute". `summarize_session` returns it whenever the reading clock is under
  `MIN_MEASURABLE_READING_MS`, because 87 words over 15ms is 348,000 wpm and the
  honest answer is to decline. Folding that 0.0 into a daily mean drags the day
  down by however many sessions were too short to time.
- `difficulty == "unknown"` is `assess_page` declining to rate a page — a slow
  read with no lookups is an interruption, not a hard page. It has no position on
  an Easy/Medium/Hard axis at all.

So both are excluded from their averages, and a day with no measurable sample
reports `None`. The chart draws a gap. A gap is true; a zero is not.

Calendar days, not rolling windows
----------------------------------
"Last 7 Days" is today's local midnight minus six days — seven calendar buckets —
rather than the previous 168 hours. A rolling window moves a session between
buckets depending on the hour the page is loaded, so the same reading appears on
different bars in the morning and the evening. The frontend mock used rolling
windows; this is a deliberate change, and it is why a session read at 23:50
counts for that day rather than tomorrow.

Days with no reading are omitted rather than emitted as zero, matching what the
charts already rendered: an unread Tuesday is an absent point, not a claim that
the reader read nothing for zero minutes.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from backend.app.modules.database.models import Session as SessionRow
from backend.app.modules.database.models import as_utc
from backend.app.modules.reading_speed.models import DifficultyLevel

# The four ranges the Analysis page's time filter offers, as a count of calendar
# days ending today. `None` is All Time — every row, however old.
RANGE_DAYS: dict[str, int | None] = {"today": 1, "week": 7, "month": 30, "all": None}

# Difficulty as a position on a three-point axis, which is what a line chart
# needs. UNKNOWN is deliberately absent: it is not a fourth point on the scale,
# it is the absence of a reading, and `KeyError` is the right outcome for code
# that tries to plot it.
DIFFICULTY_SCORES: dict[DifficultyLevel, int] = {
    DifficultyLevel.LOW: 1,
    DifficultyLevel.MEDIUM: 2,
    DifficultyLevel.HIGH: 3,
}
SCORE_LEVELS: dict[int, DifficultyLevel] = {
    score: level for level, score in DIFFICULTY_SCORES.items()
}


@dataclass(frozen=True)
class DayReading:
    """One day's reading, as the five charts need it.

    `day_start_ms` is the reader's local midnight in epoch milliseconds, not a
    formatted date. The label is the browser's job: it is the only part of this
    system that knows the reader's locale, and a server that formats "Aug 26"
    formats it in the server's language forever.

    `wpm` and `difficulty` are `None` for a day whose sessions were all
    unmeasurable — see the module docstring. Every other field is a plain sum and
    is `0` at worst.
    """

    day_start_ms: int
    minutes: int
    pages: int
    words: int
    lookups: int
    wpm: int | None
    difficulty: DifficultyLevel | None


def daily_history(db: OrmSession, *, range_key: str) -> list[DayReading]:
    """Every day in `range_key` that has reading on it, oldest first.

    Raises `KeyError` on an unknown range rather than silently defaulting to a
    week — the route validates the value first, and a typo reaching here means
    the two lists have drifted apart.
    """

    days = RANGE_DAYS[range_key]
    query = select(SessionRow)
    if days is not None:
        query = query.where(SessionRow.created_at >= _cutoff(days))

    by_day: dict[date, list[SessionRow]] = defaultdict(list)
    for row in db.scalars(query):
        by_day[_local_day(row.created_at)].append(row)

    return [_summarise(day, rows) for day, rows in sorted(by_day.items())]


# ------------------------------------------------------------------- day handling


def _local_midnight(day: date) -> datetime:
    """The start of `day` in the reader's zone, as an aware datetime.

    `.astimezone()` on a naive value resolves the offset *for that date*, which is
    what makes this correct across a daylight-saving change. Subtracting a
    `timedelta` from an already-aware midnight would keep today's offset and land
    an hour out on the day the clocks move.
    """

    return datetime(day.year, day.month, day.day).astimezone()


def _local_day(moment: datetime) -> date:
    """Which calendar day a stored timestamp falls on, for the reader."""

    return as_utc(moment).astimezone().date()


def _cutoff(days: int) -> datetime:
    """Naive-UTC start of the window, for comparison against the stored column.

    Naive because that is what SQLite holds; see `models.as_utc`. Comparing an
    aware datetime against a naive column is either an error or, worse, silently
    off by the local offset.
    """

    first = datetime.now().astimezone().date() - timedelta(days=days - 1)
    return _local_midnight(first).astimezone(timezone.utc).replace(tzinfo=None)


# -------------------------------------------------------------------- aggregation


def _level(row: SessionRow) -> DifficultyLevel:
    """The row's stored verdict, treating anything unrecognised as unrated."""

    try:
        return DifficultyLevel(row.difficulty)
    except ValueError:  # a level written by an older build
        return DifficultyLevel.UNKNOWN


def _round_half_up(mean: float) -> int:
    """Round a difficulty mean to a point on the axis, ties going to the harder.

    Explicit rather than `round()`, which banker's-rounds: `round(2.5)` is 2, so a
    day of one Medium and one Hard would report Medium. Every other tie-break in
    TaleTrace resolves toward the harder read — `assess_page` does, and
    `session_difficulty` does — and a day is not easier than the hardest thing in
    it.
    """

    return math.floor(mean + 0.5)


def _summarise(day: date, rows: list[SessionRow]) -> DayReading:
    """Fold one day's sessions into a single point.

    Sums for the counters — two sittings on a Tuesday read the sum of their
    pages — and means for the two rates, since a day does not have a total wpm.
    """

    paces = [row.session_wpm for row in rows if row.session_wpm > 0]
    scores = [
        DIFFICULTY_SCORES[level]
        for level in map(_level, rows)
        if level in DIFFICULTY_SCORES
    ]

    return DayReading(
        day_start_ms=int(_local_midnight(day).timestamp() * 1000),
        minutes=round(sum(row.reading_duration_ms for row in rows) / 60_000),
        pages=sum(row.pages_read for row in rows),
        words=sum(row.words_read for row in rows),
        lookups=sum(row.lookup_count for row in rows),
        wpm=round(sum(paces) / len(paces)) if paces else None,
        difficulty=SCORE_LEVELS[_round_half_up(sum(scores) / len(scores))] if scores else None,
    )
