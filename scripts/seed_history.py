"""The canonical reading history, and the switch that wipes the old one.

    python scripts/seed_history.py            # refresh the seeded corpus
    python scripts/seed_history.py --reset    # delete EVERY session first
    python scripts/seed_history.py --list     # show what is there, change nothing

Why this exists
---------------
The Analysis page draws five charts from persisted `sessions` rows, and it needs
history across several dates to have anything to draw. A device that has been read
on for one afternoon has one bar. So this writes a fixed set of sessions dated
back from today — enough that Today, Last 7 Days, Last 30 Days and All Time each
select a different set of rows and can each be told apart on screen.

Synthetic, deterministic, and marked as such
--------------------------------------------
Every row carries `source_reference = "seed:reading-history"`. That is the whole
provenance mechanism: `--reset` and every refresh find seeded rows by that marker
and never by inspecting their numbers. There is deliberately no
"delete sessions with fewer than N words" rule anywhere in this file — a
legitimate short read (a page at breakfast; the four-page synthetic run) has a
small word count too, and a threshold that removes bad data by shape removes good
data by the same shape.

Nothing is random. Same input, same rows, same charts, on any machine, any day —
the dates are day *offsets* from today rather than fixed calendar dates, so the
corpus cannot go stale in a week and start failing the "Today" filter.

The numbers are computed, not typed
-----------------------------------
Each entry below says how the reading went — pace, pages, lookups — and the row
is then produced by the same code that measures a real session:
`summarize_session` over a `ProgressSnapshot` and one `PageObservation` per page,
written by `record_session`. So `session_wpm`, `words_read`, the durations and the
difficulty verdict all come out of the real implementation. Two consequences worth
knowing:

- The pace is not asserted, it is *achieved*: a page read slower than the baseline
  predicts is what makes it slow, so `difficulty` is `assess_page`'s conclusion
  about these timings and not a word chosen here. That is why the corpus is also a
  live check on the measurement chain — if `summarize_session` ever stops agreeing
  with the table below, this script's printout says so.
- `baseline_wpm` on every seeded row is `SEED_BASELINE_WPM`, a local constant,
  never the reader's real calibration. The reader's own baseline is left untouched
  (seeding a history must not move a measured pace), and using it would make the
  corpus differ between machines.

What is *not* here is AI content: `summary` records what the row is, and
`review_payload` is empty. Flashcards and quiz questions come from a real Groq
call on a real session, and inventing plausible ones is exactly the kind of thing
this corpus exists to replace. Run `scripts/golden_session.py` for rows with real
review content in them.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from backend.app.modules.database.models import Folder  # noqa: E402
from backend.app.modules.database.models import Session as SessionRow  # noqa: E402
from backend.app.modules.database.models import as_utc  # noqa: E402
from backend.app.modules.database.recording import record_session  # noqa: E402
from backend.app.modules.database.session import db_session, engine, init_db  # noqa: E402
from backend.app.modules.reading_speed.analytics import (  # noqa: E402
    PageObservation,
    summarize_session,
)
from backend.app.modules.reading_speed.models import (  # noqa: E402
    CalibrationMethod,
    ProgressSnapshot,
    ReadingBaseline,
)

# The marker every seeded row carries, and the only way they are ever identified.
SEED_MARKER = "seed:reading-history"

# The reader this corpus was measured against: 200 wpm, MEASURED, so
# `baseline.is_evidence` is True and `assess_page` will actually rate a page. A
# DEFAULT baseline is an assumption, and analytics rightly refuses to conclude
# anything from a deviation against a guess — every page would come back UNKNOWN
# and the difficulty chart would be empty.
SEED_BASELINE_WPM = 200.0

# A paperback page. Fixed so pages and words stay in a believable ratio: the words
# are what the pace is measured over, and 260 of them at 150 wpm is the ~1.7
# minutes a real page took on the rig, not the half hour the old mock invented.
WORDS_PER_PAGE = 260


@dataclass(frozen=True)
class Reading:
    """One sitting, as a person would describe it.

    `days_ago` is an offset from today, not a date. `wpm` is the pace the reader
    actually held, which — measured against `SEED_BASELINE_WPM` — is also what
    decides how slow the pages look; `lookups` is the corroborating friction. Those
    two together are what `assess_page` turns into a difficulty, so the comment on
    each row below says which verdict it is aiming at.
    """

    days_ago: int
    name: str
    folder: str | None
    wpm: int
    pages: int
    lookups: int

    @property
    def words(self) -> int:
        return self.pages * WORDS_PER_PAGE

    @property
    def reading_ms(self) -> int:
        return round(self.words / self.wpm * 60_000)


# The corpus. The first seven rows are one per day back to a week ago, so "Today"
# selects one row, "Last 7 Days" selects seven, and the two bracket each other.
# Then two rows inside 30 days but outside 7, and two outside 30 days, so "Last 30
# Days" and "All Time" also differ from each other and from the week.
#
# Pace against a 200 wpm baseline is what sets the difficulty: at 150 wpm a page
# runs 33% over its predicted time, which is slow enough to count once a lookup
# corroborates it; at 110 wpm it runs 82% over, and four lookups a page make that
# HIGH. Verdicts in the comments are `assess_page`'s, and the script prints what it
# actually got so a drift between the two is visible rather than assumed.
CORPUS: tuple[Reading, ...] = (
    # --- this week, matching the agreed demo table -------------------------------
    Reading(0, "History — The Industrial Age", None, 150, 2, 2),  # Medium
    Reading(1, "English — The Last Lesson", "English", 175, 2, 1),  # Easy
    Reading(2, "Biology — Chapter 3, Genetics", "Biology", 140, 3, 6),  # Medium
    Reading(3, "English — First Flight, Ch. 4", "English", 195, 4, 0),  # Easy
    Reading(4, "Biology — Chapter 2, Photosynthesis", "Biology", 150, 2, 2),  # Medium
    Reading(5, "Biology — Chapter 1, Cell Structure", "Biology", 165, 3, 4),  # Easy
    Reading(6, "Reading Session", None, 180, 2, 1),  # Easy
    # --- inside 30 days, outside 7 ----------------------------------------------
    # Two sittings on the same day: the charts must add their pages and average
    # their pace, not show the later one and drop the earlier.
    Reading(12, "Chemistry — Reaction Rates", None, 160, 3, 3),  # Medium
    Reading(12, "Chemistry — Reaction Rates (evening)", None, 190, 1, 0),  # Easy
    Reading(21, "Chemistry — Organic Nomenclature", None, 110, 2, 8),  # Hard
    # --- outside 30 days, All Time only -----------------------------------------
    Reading(45, "English — Poetry, Unit 2", "English", 205, 5, 1),  # Easy
    Reading(68, "History — Revision Notes", None, 190, 3, 2),  # Easy
)

FOLDERS = ("Biology", "English")

# Where the rows land. Read off the engine rather than rebuilt from settings, so a
# `DATABASE_URL` pointing somewhere else is backed up and reported honestly instead
# of this script quietly reassuring somebody about a file it never touched.
DB_PATH = Path(engine.url.database) if engine.url.database else None


# --------------------------------------------------------------------- the numbers


def _baseline() -> ReadingBaseline:
    return ReadingBaseline(
        reader_id="seed",
        baseline_wpm=SEED_BASELINE_WPM,
        calibrated=True,
        method=CalibrationMethod.MEASURED,
    )


def _analytics(reading: Reading):
    """Run one entry through the real session summary.

    The lookups are spread evenly over the pages rather than piled onto the first,
    because `assess_page` weighs friction *per page*: eight lookups on page one and
    none on page two is one hard page and one unremarkable one, which is a
    different session from two consistently hard pages.

    Reading time is split evenly too, so each page deviates from its prediction by
    the same ratio the session did — the alternative is a session whose pages do
    not add up to it, which is a lie in a corpus built to stop lies.
    """

    per_page_ms = reading.reading_ms // reading.pages
    observations = [
        PageObservation(
            page_index=index + 1,
            words=WORDS_PER_PAGE,
            reading_ms=per_page_ms,
            # Round-robin, so 8 lookups over 2 pages is 4 and 4, and 3 over 2 is
            # 2 and 1 — the remainder lands on the earlier pages.
            lookups=reading.lookups // reading.pages + (1 if index < reading.lookups % reading.pages else 0),
        )
        for index in range(reading.pages)
    ]

    snapshot = ProgressSnapshot(
        session_id=f"seed-{reading.days_ago}-{reading.wpm}",
        reader_id="seed",
        elapsed_reading_ms=reading.reading_ms,
        # A little longer than the reading clock: every session has some time in it
        # that was not reading. `wall_duration_ms` is the whole sitting.
        elapsed_wall_ms=round(reading.reading_ms * 1.15),
        words_confirmed=reading.words,
        pages_visited=reading.pages,
        lookup_count=reading.lookups,
        meaning_mode_count=reading.lookups,
    )

    return summarize_session(snapshot, _baseline(), observations=observations)


# ------------------------------------------------------------------- the database


def _backup() -> Path | None:
    """Copy the database aside before anything is deleted.

    Named with a timestamp rather than overwriting one backup, because the second
    run of `--reset` is exactly when the first backup matters. Lives under
    `.taletrace_cache/`, which is gitignored — a database full of what somebody
    read does not belong in the history.
    """

    if DB_PATH is None or not DB_PATH.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = ROOT / ".taletrace_cache" / f"taletrace.db.backup-{stamp}"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(DB_PATH, target)
    return target


def _describe(rows: list[SessionRow]) -> None:
    if not rows:
        print("  (none)")
        return
    for row in rows:
        marker = "seed" if row.source_reference == SEED_MARKER else "real"
        # Shown in the reader's zone, the same way the charts bucket it. Printing
        # the stored naive-UTC value directly would date an evening session a day
        # late for anyone west of Greenwich and disagree with the screen.
        when = as_utc(row.created_at).astimezone()
        print(
            f"  {when:%Y-%m-%d}  {marker}  {row.words_read:>5} words  "
            f"{row.pages_read:>2}p  {row.session_wpm:>6.1f} wpm  "
            f"{row.difficulty:<8} {row.name}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--reset",
        action="store_true",
        help="delete every session first, not just the seeded ones",
    )
    parser.add_argument("--list", action="store_true", help="print the sessions and stop")
    args = parser.parse_args()

    init_db()

    with db_session() as db:
        existing = list(db.scalars(select(SessionRow).order_by(SessionRow.created_at)))

        if args.list:
            print(f"{DB_PATH} — {len(existing)} session(s)")
            _describe(existing)
            return 0

        backup = _backup()
        print(f"backup: {backup}" if backup else "backup: skipped (no database yet)")

        doomed = existing if args.reset else [
            row for row in existing if row.source_reference == SEED_MARKER
        ]
        if doomed:
            print(f"removing {len(doomed)} session(s):")
            _describe(doomed)
            for row in doomed:
                db.delete(row)
            db.flush()

        folders = {
            name: db.scalar(select(Folder).where(Folder.name == name)) or Folder(name=name)
            for name in FOLDERS
        }
        for folder in folders.values():
            db.add(folder)
        db.flush()

        today = datetime.now().astimezone().replace(hour=20, minute=0, second=0, microsecond=0)
        print(f"writing {len(CORPUS)} session(s):")
        for reading in CORPUS:
            row = record_session(
                db,
                analytics=_analytics(reading),
                name=reading.name,
                source_reference=SEED_MARKER,
            )
            # The one thing `record_session` cannot set, and the only reason this
            # script exists: a corpus dated entirely today tests one of the four
            # time filters. Both columns, because `created_at` is what the charts
            # bucket by and `ended_at` is what the session claims about itself.
            #
            # Converted to naive UTC, which is what SQLite holds — see
            # `models.as_utc`. Assigning the aware local value writes the local wall
            # clock into a column everything downstream reads as UTC, which shifts
            # every seeded row by the machine's offset and, east of Greenwich,
            # backdates the evening ones into yesterday.
            moment = today - timedelta(days=reading.days_ago)
            row.created_at = moment.astimezone(timezone.utc).replace(tzinfo=None)
            row.ended_at = row.created_at
            row.folder_id = folders[reading.folder].id if reading.folder else None
            row.summary = (
                f"Seeded demo session ({SEED_MARKER}). No AI review was run for this "
                f"row, so it has no flashcards or quiz questions — those come from a "
                f"real session with Meaning Mode lookups in it."
            )
            print(
                f"  {moment:%Y-%m-%d}  {row.words_read:>5} words  {row.pages_read:>2}p  "
                f"{row.session_wpm:>6.1f} wpm  {row.difficulty:<8} {reading.name}"
            )

    print("\nDone. Open the Analysis page, or check the API directly:")
    print("  curl http://127.0.0.1:8000/api/analysis?range=week")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
