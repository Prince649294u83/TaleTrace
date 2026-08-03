"""Constants that cross module boundaries.

Only values more than one module has to agree on. A constant used in a single
module belongs in that module — pulling it here would make an internal tuning
decision look like a contract, and the next person would be reluctant to change
it.

The reading-speed thresholds are the counter-example worth naming: SLOW_RATIO,
FRICTION_FOR_HIGH and friends stay in `reading_speed.analytics` because nobody
else may act on them. Difficulty is Reading Speed's conclusion to draw.
"""

# The default page a session opens on. Pages are 1-based because they name
# physical pages a reader can see, while paragraph and sentence indices are
# 0-based because they are positions within text. Mixing the two is a common
# off-by-one, so both conventions are stated here once.
FIRST_PAGE_INDEX = 1

# How often a live view refreshes. Used by the dashboard and by any future UI
# poller. One second is chosen because the numbers it shows (pace, deviation)
# move slowly enough that faster is noise, and slower feels frozen.
LIVE_REFRESH_MS = 1_000

# Milliseconds of missing signal before a consumer treats a session as idle
# rather than merely quiet. Longer than LIVE_REFRESH_MS by a wide margin so a
# single dropped frame never trips it.
SESSION_IDLE_AFTER_MS = 30_000

# Version number meaning "no OCR frame has been merged for this page yet".
# Consumers compare versions to reject stale content, so the initial value has to
# be lower than any real one.
UNVERSIONED_CONTENT = 0
