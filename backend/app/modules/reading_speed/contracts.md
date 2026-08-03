# Reading Speed — Module Contract

A **complementary prediction service**. It answers one question — *where would
this reader be by now, at their established pace?* — and nothing else.

Three prohibitions define the module. They are not guidelines; every design
decision below follows from them.

| Reading Speed never… | Who does instead |
| --- | --- |
| owns the reading session | Session / Reading Engine |
| owns the reading pointer | Reading Engine (Gesture moves it) |
| controls TTS | Audio Engine |

Nothing exported from this module can move a reader, change playback, or modify
content. `interfaces.py` has no `set_pace()`, no `advance()`, no `seek()`. If a
future change wants one, the change is wrong.

## Files

| File | Role |
| --- | --- |
| `models.py` | Data contracts. No logic, no I/O. |
| `calibration.py` | Establishes and moves a baseline. Inactive during reading. |
| `predictor.py` | Pure prediction. The heart of the module. |
| `analytics.py` | After-the-fact difficulty and session summary. |
| `tracker.py` | The only mutable state: clocks and counters for one session. |
| `service.py` | Composition + per-session registry. One public entry point. |
| `interfaces.py` | Protocols. Documentation that tests enforce. |
| `router.py` / `schemas.py` | Thin HTTP transport. No arithmetic. |
| `placeholders.py` | Zero-confidence shell for wiring before the real service. |

## The three kinds of number

Conflating these is how adaptive systems end up with a speed estimate that
chases itself.

- **Baseline** — *established*. Permanent, deliberately hard to move, frozen
  (`ConfigDict(frozen=True)`). Changed only by explicit recalibration or a
  session-end blend.
- **Progress** — *observed*. Temporary, dies with the session, and carries
  **no WPM field**. That absence is the point: a snapshot holding its own pace
  would be a second source of truth competing with the baseline.
- **Prediction / Analytics** — *derived*. Nothing stores them. Computed from the
  two above plus a content map, so they cannot drift out of sync with their
  inputs.

## Baseline lifecycle

Three origins, in descending order of trustworthiness. The origin is recorded
because the same number means different things depending on where it came from:

```
measure a passage  ->  MEASURED   evidence   (180 words / 60s = 180 wpm)
ask the reader     ->  MANUAL     a claim    (readers overestimate)
assume             ->  DEFAULT    a guess    (200 wpm, so session one shows something)
blend a session    ->  ADAPTED    evidence
```

`is_evidence` is True only for MEASURED and ADAPTED. Predictions are produced
against any baseline — a UI needs something to display from the first second —
but deviation measured against a guess is not a finding, and `analytics` refuses
to infer difficulty from one.

Calibration **raises rather than clamps**. A passage yielding 1500 wpm was not
read; silently recording 1200 would convert a detectable mistake into a permanent
wrong baseline.

### The baseline does not move during reading

This is the mistake the module is built to avoid. A baseline that updates live is
a baseline the reader is being compared against *while it moves*, which makes
deviation meaningless — you can no longer tell whether the reader sped up or the
yardstick shrank.

`adapt()` runs at session end, if at all:

- **80% old + 20% new.** One distracted session shifts an established pace by a
  few percent instead of redefining it.
- **Evidence gate.** At least 60s of reading and 150 words, or the baseline is
  returned untouched. Two sentences and a closed book is noise.
- **Uncalibrated baselines are replaced, not blended.** Averaging a real
  measurement with a number we invented dilutes it with nothing.
- **`suggest_baseline()` computes without applying**, so accepting the change can
  belong to the reader. `finish_session()` never moves a baseline on its own —
  otherwise reading a summary twice would move it twice.

## Prediction

Pure functions. Same inputs, same output, no stored state, no I/O, no calls into
any other engine.

```
input   baseline, progress snapshot, content map, session origin offset
output  expected pointer / word / finish time / page finish, deviation, confidence
```

**No prediction loop.** A prediction is a pure function of its inputs, so
recomputing on read costs less than keeping a 1 Hz task alive — and gives an
answer that is current by construction rather than up to a tick stale. A loop
would add a task to cancel, a drift window, and asyncio to every test, in
exchange for nothing. `GET .../prediction` is safe to poll as often as a UI
redraws.

### Sign conventions

The two deviations point opposite ways on purpose:

```
deviation_words > 0   further along than predicted   (ahead)
deviation_ms    > 0   took longer than predicted     (behind)
```

The second matches how a reader describes it: *"expected 90 seconds, actually
took 140"* is +50.

### Two clocks

Mirrors the audio engine. `elapsed_reading_ms` excludes paused and Meaning Mode
intervals; `elapsed_wall_ms` is total session time. Prediction uses the reading
clock **only**, so looking up a word never makes the reader appear slower. That
is the entire reason both exist.

### Session origin

Predictions measure from `origin_word_offset`, where the session began within the
content — not from word zero. A reader who opens the book at page 40 has not read
the preceding 39 pages, and measuring against zero would report them permanently
and absurdly behind.

### Confidence

Always returns a prediction; reports unreliability rather than withholding.
Degraded by an unmeasured baseline (×0.4), under 30s of reading (scaled), and an
empty content map (0.0). Low confidence marks results that should not drive
analytics or be shown as fact.

## Difficulty: evidence before verdict

**Time alone cannot identify a hard page.** A reader who spent four minutes
because the vocabulary was dense and one who spent four minutes because they
answered the door produce identical timings.

So the order of inference is fixed:

1. **Deviation first.** `actual − expected` (expected 90s, actual 140s → +50s).
2. **Then corroboration.** Lookups, meaning requests, pointer corrections,
   revisits.
3. **Only then** name LOW / MEDIUM / HIGH.

A slow page with **no** friction is reported `UNKNOWN`, not `HIGH` — being away
from the book is not a reading difficulty, and calling it one would quietly
poison every downstream average. `UNKNOWN` is also returned when the baseline is
not evidence, the page is under 30 words, or no reading time was recorded.

Every verdict ships with the `evidence` tuple that produced it, so a caller can
always see why. The evidence travels beside the conclusion rather than being
discarded once the verdict is reached.

## Events are methods, not a bus

The nine session events are the nine methods on `ProgressTracker`:

| Event | Method |
| --- | --- |
| SESSION_STARTED | `session_started()` |
| SESSION_PAUSED | `session_paused()` |
| SESSION_RESUMED | `session_resumed()` |
| READING_POINTER_UPDATED | `pointer_updated()` |
| PAGE_CHANGED | `page_changed()` |
| MEANING_MODE_ON | `meaning_mode_on()` |
| MEANING_MODE_OFF | `meaning_mode_off()` |
| LOOKUP_COMPLETED | `lookup_completed()` |
| SESSION_FINISHED | `session_finished()` |

The decoupling that matters is preserved — a tracker never calls Gesture, OCR,
AI, or the Audio Engine, and cannot tell which of them is reporting — without a
message bus, which would be the only one in the codebase. **Nothing polls.**
Callers report events when they happen.

`pointer_updated()` infers a page change from the pointer, so Gesture only ever
has to report a position. Meaning Mode is counted separately from a plain pause
because the two say opposite things about the page: a user pause says nothing
about the text, while entering Meaning Mode says the reader hit something they
could not read past.

## Integration

| Module | Relationship |
| --- | --- |
| **Merge Memory** | Source of truth. Read only, never modified. Supplies `ContentMap` — counts and pointers, no strings. Stale `source_version` is rejected. |
| **Audio Engine** | Never asks "how fast is this reader?". Its `PlaybackStatistics` is *ingested* at session end. |
| **Gesture** | Reports sentence index / character offset. `corrected=True` marks a correction, which is friction evidence; ordinary forward movement is not. |
| **AI Engine** | Reports lookups and meaning requests. Never computes analytics itself. |
| **OCR** | Provides Merge Memory. Nothing else. |

### Ingesting `PlaybackStatistics`

When TTS is on, the audio engine has already counted spoken sentences and words
*while speaking them* — strictly more accurate than anything inferable from
pointer movement. Those counts are ingested rather than recomputed, for the same
reason this module adds no diagnostics of its own.

`tts_assisted` records which path was taken, because a session read aloud and a
session read silently are not comparable measurements of the same reader: TTS
paces the reader instead of the reader pacing themselves.

## Page word counts and the lagging pointer

Closing a page cannot trust the pointer alone. A reader who turns from page 1 to
page 2 read all of page 1, but the pointer commonly still sits mid-page — Gesture
reports a position when the reader *moves* it, not once per sentence. Crediting
only the words up to the pointer against the full page time would rate every
ordinary page as difficult.

So a **forward turn** counts from the page entry point to the end of the page; a
**backward move or session end** counts only as far as the pointer actually
reached. Counting from the entry point rather than the page total matters for a
session that opens mid-page: those earlier words were never read.

## Session isolation

One `ProgressTracker` per `session_id`, mirroring `AudioSessionManager`. A shared
tracker means one reader's page turn closes another reader's page — a defect no
single-reader test can expose, which is exactly how the audio engine acquired the
same bug. `GET /reading-speed/sessions` exists to make leakage visible.

An unknown `session_id` is a **404**, not an implicitly created session. A
tracker invented on demand would start its clock at that moment and report an
entirely reasonable-looking pace for a session that never began.

## Logging

Single-line events through the standard module logger configured by
`backend/app/core/logging_config.py`, shaped like the audio engine's:

```
[reading_speed:<session_id>] <event> <detail>
```

No separate diagnostics system.

## Departures from the original design sketch

Stated rather than silently applied:

- **No 1 Hz prediction loop** — replaced by compute-on-read. Same outputs; no
  task, no drift window, no asyncio in tests.
- **No event bus** — the nine events are direct methods. Identical decoupling,
  no infrastructure.
- **`SessionAnalytics` ingests `PlaybackStatistics`** instead of recomputing
  `session_average_wpm`, `pages_read`, `meaning_requests`, `reading_duration`,
  which already exist there.
- **`SentenceSpan` and `ContentMap` added** beyond the original model list. The
  predictor needs word counts and a pointer↔offset mapping supplied *from* Merge
  Memory without this module ever touching text.
- **`tracker.py` and `service.py` added** beyond the original file list, as the
  non-bus home for event handling and per-session isolation.

## Reference

Time-on-task alone is insufficient to determine learning difficulty; corroborating
interaction signals are required.

- Adapt2 adaptive learning platform — <https://adapt2.sis.pitt.edu>
- <https://doi.org/10.3390/a19020100>
