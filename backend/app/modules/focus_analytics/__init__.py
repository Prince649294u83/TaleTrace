"""Reading Focus Analysis Engine — module boundary.

Answers one question about a finished session: **which sections required more
attention than expected?** Paragraphs are the unit, because a page is too coarse
to revise from and a sentence is too small to time reliably.

It is not a distraction detector, and the naming is part of the design rather than
decoration. There is no module, class or field in this package named for
distraction; the engine is `FocusAnalyticsEngine` internally and the output is
displayed as *Reading Focus Analysis*. Where the evidence supports only "the
pointer stopped moving", the report says **possible idle time** and rates the
paragraph UNKNOWN. It never concludes anything about the reader's attention,
because a pointer and a clock cannot support that conclusion.

Everything exported here is read-only with respect to the rest of the system.
Nothing in this package can move the Reading Pointer, change playback, call the AI
Engine, trigger OCR, write to Merge Memory, or alter a Reading Speed baseline. It
observes and reports; every other module is free not to know it exists.
"""

from backend.app.modules.focus_analytics.analysis import (
    FRICTION_FOR_HIGH,
    FRICTION_FOR_MEDIUM,
    IDLE_ALLOWANCE_MULTIPLE,
    IDLE_FLOOR_MS,
    MIN_MEASURABLE_FOCUSED_MS,
    MIN_PARAGRAPH_WORDS,
    assess_paragraph,
    idle_ms_for_gap,
    revision_priority,
)
from backend.app.modules.focus_analytics.engine import FocusAnalyticsEngine
from backend.app.modules.focus_analytics.models import (
    DIFFICULTY_WEIGHT,
    FULL_SCALE_DIFFICULTY_RATIO,
    FULL_SCALE_MEANING_REQUESTS,
    FULL_SCALE_REVISITS,
    MEANING_WEIGHT,
    REVISIT_WEIGHT,
    FocusReport,
    ParagraphFocus,
    ParagraphObservation,
)

__all__ = [
    "DIFFICULTY_WEIGHT",
    "FRICTION_FOR_HIGH",
    "FRICTION_FOR_MEDIUM",
    "FULL_SCALE_DIFFICULTY_RATIO",
    "FULL_SCALE_MEANING_REQUESTS",
    "FULL_SCALE_REVISITS",
    "IDLE_ALLOWANCE_MULTIPLE",
    "IDLE_FLOOR_MS",
    "MEANING_WEIGHT",
    "MIN_MEASURABLE_FOCUSED_MS",
    "MIN_PARAGRAPH_WORDS",
    "REVISIT_WEIGHT",
    "FocusAnalyticsEngine",
    "FocusReport",
    "ParagraphFocus",
    "ParagraphObservation",
    "assess_paragraph",
    "idle_ms_for_gap",
    "revision_priority",
]
