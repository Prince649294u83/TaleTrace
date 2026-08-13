# Reading Focus Analysis — Module Contract

An **observation-only analytics engine**. It answers one question — *which
sections required more attention than expected?* — at paragraph granularity, and
nothing else.

It is deliberately **not** a distraction detector. That question — *was the
reader distracted?* — cannot be answered by a pointer and a clock, and the whole
module is shaped so that it cannot accidentally appear to answer it. Where the
evidence supports only "the pointer stopped moving", the report says **possible
idle time** and the paragraph is rated `UNKNOWN`.

Four prohibitions define the module. They are not guidelines; every design
decision below follows from them.

| Reading Focus Analysis never… | Who does instead |
| --- | --- |
| controls OCR, AI, Audio, Merge Memory or the pointer | each module's owner; the Reading Engine sequences them |
| moves or influences a Reading Speed baseline | `reading_speed.calibration` |
| reconstructs, segments or holds text | Merge Memory |
| concludes anything about the reader's attention | nothing — the data does not support it |

Nothing exported from this package can move a reader, change playback, modify
content, or alter a baseline. `FocusAnalyticsEngine` has no `set_`, no `advance`,
no `seek`, no `move_pointer`. A test asserts that absence structurally rather
than trusting this paragraph. If a future change wants one, the change is wrong —
a runtime that should slow playback on a hard paragraph reads this engine's
output and decides *in the Reading Engine*.

## Files

| File | Role |
| --- | --- |
| `models.py` | Data contracts. No logic, no I/O, no strings of the book. |
| `analysis.py` | Pure functions: observations → conclusions. No state, no clock. |
| `engine.py` | The only mutable state: per-paragraph tallies for one session. |
| `__init__.py` | The module boundary and its `__all__`. |

## Observation and conclusion are separate shapes

The split is the reason a finished session can be re-judged against a baseline
that arrived later without replaying a single event, and the reason the engine is
testable by handing it timestamps.

- **`ParagraphObservation`** — *counted*. What happened: words, elapsed time,
  idle time, meaning requests, lookups, revisits. Frozen. No verdict.
- **`ParagraphFocus`** — *concluded*. Adds expected time, both ratios, a
  `DifficultyLevel`, a Revision Priority Score, and the `evidence` tuple that
  produced them.
- **`FocusReport`** — *assembled*. Every paragraph in reading order, plus session
  totals and which baseline judged them.

`DifficultyLevel` is **borrowed** from `reading_speed.models`, not redefined. A
second four-valued difficulty enum would be two vocabularies that must agree.

## Paragraphs are the unit of analysis

A page is too coarse to revise from; a sentence is too small to time reliably.

Paragraph structure is **asked for, never derived**. Word counts come from Merge
Memory's own `content_map()`, grouped by the `paragraph_index` its
`SentenceSpan`s already carry. Those spans were built by Merge Memory's own
segmenter, so the answer to *"which paragraph contains this sentence?"* cannot
drift from the one the Audio Engine and Reading Speed are using. This engine
never splits a string and in fact never holds text at all.

## The metrics

| Metric | Definition |
| --- | --- |
| Expected Reading Time | `words ÷ baseline WPM` (via `reading_speed.predictor.expected_ms_for`) |
| Actual Reading Time | elapsed *reading* time in the paragraph |
| Idle Time | the part of a pointer gap exceeding a generous allowance |
| Reading Difficulty | `(focused − expected) ÷ expected` |
| Revision Priority | 40% difficulty + 40% meaning requests + 20% revisits, 0–100 |

### Difficulty is measured against *focused* time, not elapsed time

```
focused_ms = actual_ms − idle_ms
```

A reader who spent four minutes on a paragraph because someone spoke to them for
three did not find it hard. Scoring raw elapsed time would rank that interruption
above every genuinely difficult paragraph in the session.

`ParagraphFocus` carries **both** ratios — `difficulty_ratio` against focused
time and `elapsed_ratio` against raw time — because when they disagree, the gap
*is* the idle time.

### Idle Time is a quantity, not a flag

Only the **excess** over an allowance counts, and the allowance scales with the
text actually crossed:

```
allowance = max(expected_ms(words_crossed) × 3.0, 5000ms)
idle      = max(gap − allowance, 0)
```

A reader who paused eight seconds where six were reasonable was idle for two;
charging all eight would make every slightly slow paragraph look abandoned. The
five-second floor exists because pointer updates arrive on gestures and merges
rather than on a timer, so gaps of a second or two are the normal texture of a
session and not the reader stopping.

Continuous rather than boolean on purpose: idle time is subtracted from a
measurement, and a `was_idle` flag would force the caller to keep all of a
paragraph or discard it.

**Wording is fixed by specification, not style.** The evidence reads *"possible
idle time of Ns — reading paused longer than expected"*. It never says the reader
was distracted, and a test sweeps the whole module for that vocabulary.

### Normalise before weighting

Each component of the Revision Priority Score is scaled to 0–1 against a
full-scale value before the 40/40/20 weighting. Without that, "one meaning
request" and "a ratio of 1.0" would enter the sum on unrelated scales and
whichever happened to be numerically larger would dominate.

Two properties follow deliberately:

- **Negative difficulty contributes zero, not a negative amount.** Reading
  something quickly is not evidence it needs *less* revision than a paragraph
  with no signal, and letting it go negative would let speed cancel out real
  meaning requests.
- **`lookups` is not a term.** A completed lookup is the *resolution* of a
  meaning request, not a separate moment of confusion; counting both would score
  the same moment twice. It travels in the evidence instead.

## Evidence before verdict

Same order of inference as `reading_speed.analytics`, and for the same reason —
time alone cannot distinguish a hard paragraph from an interrupted one. It
becomes *more* true at this granularity: a paragraph is a tenth the size of a
page, so the noise in its timing is proportionally larger and a lone slow
measurement means less.

**Friction is collected first**, because friction is the only signal that does
not need a baseline to mean something: a reader who asked what a word meant
asked, whatever their pace. Time is corroboration.

A paragraph that is slow with **no** friction is `UNKNOWN`, never `HIGH`. That
single branch is the whole philosophy of the module — calling it difficult would
report every interruption in the session as a comprehension problem.

`UNKNOWN` is also returned when the baseline is not evidence, the paragraph is
under 8 words, there is no expected time, or under 500ms of reading was recorded
(the reader *crossed* the paragraph rather than read it).

### Thresholds: imported where they exist, redefined where they must be

| Constant | Origin | Why |
| --- | --- | --- |
| `SLOW_RATIO`, `VERY_SLOW_RATIO`, `FAST_RATIO` | **imported** from `reading_speed.analytics` | A ratio is scale-free. A second copy would be two numbers that must agree, and the first time one was tuned the two engines would disagree about the same reading. |
| `FRICTION_FOR_MEDIUM` = 1, `FRICTION_FOR_HIGH` = 2 | **redefined** (page-level is 1 and 3) | Same evidence, different denominator: three lookups on a page is a reader working hard; three in one paragraph is a reader who did not understand it. |

**Friction can convict where timing cannot, but never to `HIGH`.** Two meaning
requests in a four-word heading is the clearest signal a session can produce, so
an unmeasurable paragraph with friction ≥ 2 returns `MEDIUM` rather than dropping
the finding. `HIGH` requires both halves of the evidence.

## Unknown paragraphs are excluded from the ranking, not ranked last

`FocusReport.ranked` drops `UNKNOWN` paragraphs entirely. An unjudged paragraph
sitting at the bottom of a list reads as *"this one was fine"*, which is the one
thing `UNKNOWN` exists to avoid claiming. `needs_attention()` returns an empty
tuple for an easy session rather than padding to `limit`.

## Events are methods, not a bus

Mirrors Reading Speed exactly. **Nothing polls** — there is no timer and no
background task, which is what lets a fifteen-minute session be tested in a
millisecond.

| Event | Method |
| --- | --- |
| SESSION_STARTED | `session_started()` |
| READING_POINTER_UPDATED | `pointer_updated()` |
| PAGE_CHANGED | `page_changed()` → forwards to `pointer_updated()` |
| MEANING_REQUESTED / MEANING_MODE_ON | `meaning_requested()` |
| MEANING_MODE_OFF | `meaning_mode_off()` |
| LOOKUP_COMPLETED | `lookup_completed()` |
| SESSION_PAUSED | `session_paused()` |
| SESSION_RESUMED | `session_resumed()` |
| SESSION_FINISHED | `session_finished()` |

`handle(event, pointer)` is the wiring seam. Unknown and irrelevant events are
**ignored rather than raised**: this engine observes a stream it does not own, and
a new event elsewhere in the system must never be able to stop a session by
arriving here.

`page_changed` forwards rather than handling separately — the paragraph the
reader was in has ended either way, and a second closing path would be a second
chance for the tallies to disagree with the pointer.

### Two clocks, matching Reading Speed

The reading clock stops while paused or in Meaning Mode; the wall clock does not.
Looking a word up must never make the reader appear slower — it is already
counted as friction.

Measuring in *reading* time makes pause handling nearly free: the pause advances
the reading clock by zero, so the open paragraph is not charged for it and no
correction is needed on resume. A wall-clock version of this engine would need
one, and forgetting it would report every pause as idle time.

`_resume()` deliberately leaves `_last_move_ms` **alone**. Moving it to the
moment of resuming would forgive whatever gap had already accrued before the
pause, so a reader who stalled and *then* took a break would have the stall
erased — and pausing would become the way to hide idle time from the report.

### The trailing gap

Idle time is normally revealed by the *next* pointer update, which says how much
text the gap covered. The final paragraph has no next update, so
`session_finished()` closes it explicitly. Without that, a reader who stalled and
closed the book has all of it charged as reading time, and the last paragraph of
every interrupted session is the slowest thing in the report.

## Integration

| Module | Relationship |
| --- | --- |
| **Merge Memory** | Read only, through `MergeMemorySource`. Asked for paragraph structure; never written to, never second-guessed. Cached on `version`. |
| **Reading Speed** | Shares `ReadingBaseline`, `DifficultyLevel`, `expected_ms_for` and the ratio thresholds. Never moves a baseline. |
| **Reading Engine** | Notifies this engine at the same call sites that already notify Reading Speed, and is never *asked* anything by it. |
| **AI Engine** | Consumes the report. Summaries read these metrics rather than recomputing them. |
| **Audio / OCR / Gesture** | No relationship in either direction. |

### Why the Reading Engine notifies at call sites rather than through `_record()`

`_record()` looks like a single hook and is not usable as one: `PAGE_CHANGED` is
recorded *before* `move_pointer` is awaited, so an observer hooked there would be
handed the pre-turn pointer and would attribute the new page's paragraph time to
the old page.

`ReadingEngine.focus` is optional in the strongest sense — a session with no
focus engine is a complete session that produces no focus report. It is notified
beside Reading Speed and never queried, so nothing the session does can depend on
its answers. That is what makes *"observes only"* a property of the wiring rather
than a promise.

### The report is judged at the end, against the baseline as it then stands

`finish_session()` passes `speed.baseline_for(reader_id)` into `report()`. A
session that calibrated the reader part-way through knows more at the end than it
did at the start, and a report judged against the engine's construction-time copy
would return a session's worth of `UNKNOWN` for no reason.

`report(baseline)` **never stores** the override. Moving a baseline is
`calibration.adapt()`'s decision; making it a side effect of asking for a report
would mean reading a report twice changed the reader's profile — the same rule
`reading_speed.finish_session()` follows.

## Testable without hardware

Everything is injectable and nothing sleeps: the clock is a parameter, paragraph
structure comes from a real `MergeMemory`, and time advances only when an event
arrives.

Tests are built on a **real** `MergeMemory` rather than a stub returning invented
counts — a stub would test the engine against a paragraph structure the product
never produces.

The philosophy tests are the point. A version of this engine that passed every
arithmetic test and failed those would be the distraction detector the
specification exists to prevent.

`scripts/validate_pipeline.py` runs the engine against a real photograph as
stage 10, and checks what a unit test cannot: that the word counts match the
paragraphs Merge Memory actually holds, that the meaning request the analysis
counted is the one the session recorded, and that this engine's reading clock
agrees with Reading Speed's to within scheduling jitter. That last one is
compared against `tracker.snapshot().elapsed_reading_ms` rather than
`SessionAnalytics.reading_duration_ms` — when TTS is on, `analytics._totals`
prefers the Audio Engine's `reading_time_ms`, which counts time spent *speaking*
rather than time spent reading, and the two are not the same quantity.

The validation reader is never calibrated, so the harness reports every paragraph
as `UNKNOWN` and says so in a note. Calibrating one there to make the ranking
non-empty would be inventing a reader; the difficulty branches are exercised by
the unit suite, which can hand the engine a measured baseline honestly.

## Naming

Part of the design, not decoration.

| Layer | Name |
| --- | --- |
| Package / module | `focus_analytics` |
| Class | `FocusAnalyticsEngine` |
| Displayed to a reader | *Reading Focus Analysis* / *Reading Difficulty Analysis* |
| Never, anywhere | "Distraction Detection" |

A test asserts no public member of the package is named for distraction.
