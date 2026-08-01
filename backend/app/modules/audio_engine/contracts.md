# Reading Audio Engine contracts

The Reading Audio Engine coordinates synchronized narration for active reading sessions. It does **not** generate content, own the reading pointer, or decide what to read — those responsibilities belong to the Reading Engine. This engine consumes a pointer and plays the corresponding text.

---

## Responsibilities

**In scope:**
- Accept a reading pointer (page, paragraph, sentence, character offset)
- Convert text to speech via a provider abstraction
- Maintain playback state (idle, playing, paused, waiting for pointer update)
- Apply audio profiles (Normal, Adaptive, Disability, Study, Novel)
- Queue sentences so Merge Engine updates don't interrupt playback
- Pause/resume on Meaning Mode or Reading Update events
- Provide playback status and available voices
- Keep each reader's playback isolated, keyed by `session_id`
- Report playback analytics for the session that just ended

**Out of scope:**
- Reading pointer ownership (Reading Engine owns it)
- OCR or text extraction (OCR Engine)
- Deciding what to explain (AI Engine)
- Background ambience mixing (future Audio Mixer, not built yet)
- Session lifecycle and reading history across sessions (Reading Engine)

On that last point: the engine tallies what *it* did — sentences spoken, pauses, pages, pace — because only the engine can observe them. It reports that tally and forgets it. Persisting it, aggregating across sessions, and deciding what it means belong to the Reading Engine.

---

## Core Models

### `ReadingPointer`

Structured index into the current reading position, shared across every module.

```python
page_index: int          # 1-based page number
paragraph_index: int     # 0-based paragraph on that page
sentence_index: int      # 0-based sentence in that paragraph
character_offset: int    # 0-based character in that sentence (for future resume)
```

**Why indices over text matching:** `"portfolio"` might appear twice on a page. An index is unambiguous.

### `PlaybackState`

Finite state machine for playback lifecycle.

```
IDLE → READY → PLAYING → PAUSED → PLAYING → FINISHED
                    ↓                  ↑
              WAITING_FOR_POINTER ──────┘
```

- `IDLE` — no session active
- `READY` — pointer received, provider initialized, not speaking yet
- `PLAYING` — actively speaking
- `PAUSED` — suspended (Meaning Mode or user pause)
- `WAITING_FOR_POINTER` — Reading Update in progress; finish current sentence, then jump
- `FINISHED` — reached end of content

**`IDLE` and `FINISHED` are not the same thing.** `FINISHED` means this queue ran dry; the session is still open and its tally still accumulating. `IDLE` means `stop()` was called and the session is over. Only `start()` from `IDLE` begins a new tally — which is why a page turn calls `start()` without stopping first, and why `wait_for_idle()` leaves the machine in `FINISHED` rather than `IDLE`.

### `AudioProfile`

Per-mode configuration for speech characteristics.

```python
name: str              # "Normal", "Adaptive", "Disability", etc.
rate: float            # 0.5–2.0, where 1.0 is default provider speed
pitch_shift: int       # semitones, typically -10 to +10
pause_after_sentence_ms: int   # extra pause after punctuation
emphasis_level: str    # "none", "moderate", "high"
```

Profiles map to provider-specific parameters (SSML rate/prosody for Azure, `rate` param for Edge TTS, etc.).

### `SentenceChunk`

One unit of speech queued for playback.

```python
text: str
pointer: ReadingPointer   # where this sentence starts
duration_estimate_ms: int | None
```

### `PlaybackStatistics`

Analytics for one reading session, returned inside `PlaybackStatus` and again as a final snapshot after `stop()`.

```python
sentences_spoken: int
words_spoken: int
characters_spoken: int
pages_read: int
pause_count: int
meaning_mode_count: int
reading_updates: int
queue_refreshes: int
stale_updates_rejected: int
playback_time_ms: int     # wall clock since playback began
reading_time_ms: int      # excludes every paused interval
average_wpm: float        # derived from reading_time_ms
```

**Two clocks, deliberately.** `reading_time_ms` stops while paused, so a long Meaning Mode explanation does not make the reader look slow. `playback_time_ms` is wall time. `average_wpm` uses reading time, the only one that reflects actual pace.

**A sentence counts once it finishes uninterrupted.** One cut off by a pause is requeued and spoken again, so counting at synthesis time would double it.

**Statistics belong to the session, not to one `start()` call.** A page turn calls `start()` again to rebuild the queue, so the tally must carry over. Only a `start()` from `IDLE` — the state `stop()` leaves behind — begins a new session and resets. See Implementation Note 7.

### `PlaybackStatus`

What `GET /audio/status` returns.

```python
state: PlaybackState
session_id: str | None
pointer: ReadingPointer | None
current_sentence: str | None
profile_name: str | None
provider: str | None
voice_id: str | None
queued_sentences: int
queue_version: int              # see Implementation Note 6
pause_reason: PauseReason | None
elapsed_reading_ms: int         # retained for existing callers
statistics: PlaybackStatistics
error: str | None
```

---

## Speech Provider Abstraction

Every provider implements:

```python
async def synthesize(request: SpeechRequest) -> SpeechResponse
async def get_available_voices() -> list[Voice]

@property
def provider_name(self) -> str
```

`SpeechRequest` carries `text`, the `AudioProfile`, and an optional `voice_id`. `SpeechResponse` carries `audio: bytes | None`, `content_type`, `provider`, and `error`.

**Why a response object rather than raw bytes:** a provider that fails must be able to say why without raising, and one that speaks directly to the sound device returns no bytes at all. `SpeechResponse(audio=None, error=None)` is a valid, successful result — see Audio Sinks below.

Providers are **swappable without changing the playback engine**. Built-in providers:

1. **EdgeSpeechProvider** — high-quality neural voices, cloud-dependent, free
2. **OfflineSpeechProvider** (pyttsx3) — always available, robotic, local fallback
3. **FakeSpeechProvider** — returns instantly, used by tests and the simulator's smoke run

Future: `AzureSpeechProvider`, `ElevenLabsProvider`, etc.

### Why two real providers from day one

Edge TTS [breaks when Microsoft rotates auth tokens](https://github.com/rany2/edge-tts/issues/290), and **datacenter IPs are blocked regardless of a valid token**. If you demo from a cloud host or the venue routes through something that looks like a datacenter, your demo dies. The offline provider is insurance.

---

## Audio Sinks

A provider produces audio; a **sink** decides where it goes.

```python
async def play(audio: bytes, *, content_type: str = "audio/mpeg") -> None
async def stop() -> None
```

- `NullAudioSink` — discards audio. Used by tests, and by `OfflineSpeechProvider`, which speaks to the device itself and returns no bytes.
- `LocalAudioSink` — plays through whichever player the machine has.

Two requirements make this more than a wrapper around a subprocess:

1. **`play()` must not return until playback finishes.** The engine treats it as the duration of the sentence. A sink that returns early makes playback appear instantaneous — the queue drains before any event can interrupt it.
2. **`stop()` must cut playback off mid-sentence.** This is what Meaning Mode is.

Keeping the sink separate from the provider is what lets the ESP32 speaker path replace local playback later without touching a provider. See Implementation Note 8 for the platform trap here.

---

## Multi-Session Support

Playback state is keyed by `session_id`. `AudioSessionManager` maps an id to its own `PlaybackEngine`, creating one on first use.

```python
manager.get(session_id) -> PlaybackEngine   # creates on first use
manager.has(session_id) -> bool
manager.session_ids() -> list[str]
manager.statuses() -> list[PlaybackStatus]
await manager.close(session_id) -> bool     # stops playback, discards the engine
await manager.close_all() -> int
```

**Why this exists:** a single module-level engine meant two readers shared one playback state. One reader's Meaning Mode pause would stop the other's book, and their pointers would fight over the same queue. Nothing in the API surface revealed the problem — every route worked correctly with one reader.

`session_id` is a query parameter on every route, defaulting to `"default"`, so a single-reader client can ignore it entirely. Two clients that both omit it share one engine; distinct ids keep them isolated.

`engine_factory` is injectable, so tests and the simulator supply fake providers without reaching into engines after construction.

**Reclamation.** Above a soft limit of 32 sessions, `get()` prunes engines in `IDLE` or `FINISHED` before creating another. `PAUSED` is deliberately never pruned — that reader is mid-book in Meaning Mode and still expects to resume. The prototype has no session expiry, so `DELETE /audio/session` on Camera OFF is what actually keeps the registry bounded.

---

## Event Flow

### Start Session

```
Reading Engine
    ↓
POST /audio/start (with initial pointer)
    ↓
Playback Engine: IDLE → READY → PLAYING
```

### Reading Update (user adjusts pointer mid-session)

```
Gesture Engine detects new sentence
    ↓
Reading Engine updates pointer
    ↓
POST /audio/seek (with new pointer)
    ↓
Playback Engine: PLAYING → WAITING_FOR_POINTER
    ↓
Current sentence finishes
    ↓
Jump to new pointer
    ↓
WAITING_FOR_POINTER → PLAYING
```

**Critical:** Never interrupt mid-sentence. Finish, then jump.

### Meaning Mode

```
Meaning Mode ON
    ↓
POST /audio/pause
    ↓
PLAYING → PAUSED (reading timer also frozen)
    ↓
AI explanation appears
    ↓
Meaning Mode OFF
    ↓
POST /audio/resume
    ↓
PAUSED → PLAYING (same pointer, no loss)
```

### Merge Memory Update (Reading Engine has new text)

```
Merge Engine updates paragraph
    ↓
Reading Engine checks: is the current playing sentence affected?
    ↓
No → ignore (Sentence Queue already has it)
    ↓
Yes → POST /audio/refresh-queue (text + source_version)
    ↓
source_version <= last applied? → reject, return applied=false
    ↓
Otherwise: keep current sentence, rewrite everything after it, continue
```

The queue acts as a buffer so OCR improvements don't restart playback. The sentence in flight is already dequeued, so it is never disturbed — only what follows it is rewritten.

`source_version` is Merge Memory's own version for that text. OCR frames travel over HTTP and can arrive reordered; applying an older frame after a newer one would regress the text the reader is about to hear. Omit it and every refresh applies, which is fine for a single in-process caller. See Implementation Note 6.

### Page Turn

```
Similarity < threshold
    ↓
Reading Engine commits previous page, resets pointer
    ↓
POST /audio/start (new page, pointer reset)   ← not /stop first
    ↓
Playback Engine rebuilds the queue, keeps the session tally
```

**Do not call `/audio/stop` first.** `stop()` ends the session: it snapshots the final statistics and returns to `IDLE`, and the next `start()` then begins a fresh tally. A page turn is mid-session, so calling `start()` directly is what preserves `sentences_spoken`, `pages_read`, and reading time across the turn.

### Camera OFF

```
Camera OFF
    ↓
POST /audio/stop          → final statistics snapshot, state IDLE
    ↓
GET /audio/status         → read PlaybackStatistics for the summary
    ↓
DELETE /audio/session     → discard the engine
    ↓
Reading Engine generates summary/flashcards/quiz
```

`stop()` snapshots statistics *before* returning to `IDLE`, because that transition zeroes the reading clock. Without `DELETE`, engines accumulate for the life of the process.

---

## API Endpoints

All routes under `/audio`. Every route except `/profiles` and `/sessions` takes a `session_id` query parameter, defaulting to `"default"`.

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/start` | Begin playback from a pointer (also used for page turns) |
| `POST` | `/pause` | Suspend playback (Meaning Mode, user pause) |
| `POST` | `/resume` | Continue from where paused |
| `POST` | `/stop` | End the session, snapshot statistics, return to IDLE |
| `POST` | `/seek` | Jump to a new pointer (Reading Update) |
| `POST` | `/refresh-queue` | Rewrite pending sentences after a Merge update |
| `POST` | `/profile` | Swap the audio profile; applies to the next sentence |
| `POST` | `/voice` | Swap the voice; applies to the next sentence |
| `GET` | `/status` | State, pointer, queue version, statistics |
| `GET` | `/voices` | Available voices from the active provider |
| `GET` | `/profiles` | Available audio profiles (not session-scoped) |
| `GET` | `/sessions` | Every live session, for confirming isolation |
| `DELETE` | `/session` | Stop a session and discard its engine |

Mutating routes return `PlaybackStateResponse`: `state`, `session_id`, `pointer`, `profile_name`, `queue_version`, and `applied`. `applied` is `false` on `/refresh-queue` when a stale `source_version` was rejected — the request succeeded, but nothing changed.

Thin router — all logic lives in the engine.

---

## Integration with Reading Engine

The Reading Engine is the **source of truth** for:
- Current reading pointer
- Merge Memory (clean text after OCR reconstruction)
- Session lifecycle (active, paused, ended)
- Reading history across sessions

The Audio Engine holds **playback state only, scoped to a session id**:
- Accepts pointers and plays the corresponding text
- Reports playback status and statistics when asked
- Never queries OCR, Camera, or Gesture modules directly
- Never persists anything; every engine dies with the process

It is not stateless — it holds a queue, a pointer, a state machine, and a tally per session. What it does not hold is anything that outlives the session or that another module owns.

**Data flow:**

```
Reading Engine owns:
    ReadingPointer
    Merge Memory (+ its version)
    Session lifecycle

Audio Engine consumes:
    session_id
    ReadingPointer (read-only)
    text: str (from Merge Memory)
    source_version: int (from Merge Memory)

Audio Engine emits:
    PlaybackStatus (state, pointer, queue_version)
    PlaybackStatistics (final snapshot on stop)
```

### Integration checklist

For whoever wires the Reading Engine to this module:

1. Pass a real `session_id` per reader. Do not let two readers fall through to `"default"`.
2. Call `/audio/start` for page turns. Do not call `/audio/stop` first — it resets the tally.
3. Send `source_version` with every `/audio/refresh-queue` call, and check `applied` in the response.
4. Read statistics from `/audio/status` after `/audio/stop`, before `DELETE /audio/session`.
5. Call `DELETE /audio/session` on Camera OFF. Nothing else reclaims engines.

---

## Future Extensions (out of scope for prototype)

- Character-level resume (requires word-boundary events from provider)
- Background ambience mixing (Novel Mode music + narration)
- Emotional SSML (pitch/rate modulation per scene mood)
- Prefetch next sentence while current one plays
- Voice cloning for character dialogue
- Multi-language switching mid-session

---

## Testing Strategy

Two layers, because they catch different things.

### Unit tests (`tests/test_audio_engine.py`)

Verify engine behaviour, not audio quality:

- Pointer advancement after sentence completion
- Pause/Resume preserves pointer
- Meaning Mode suspends playback without state loss
- Reading Update jumps after finishing current sentence
- Merge updates don't restart playback unnecessarily
- Stale `source_version` refreshes are rejected
- Page changes reset the pointer but carry statistics
- Sessions with distinct ids do not share state
- Provider swap (Edge → offline) changes nothing in playback logic
- State transitions follow the FSM (no invalid transitions)

Mock the speech provider and inject a fake clock; tests run offline, with no network and no real waiting.

### Session rehearsal (`backend/demo/`)

```
python -m backend.demo.simulate_reading                      # fake provider, compressed time
python -m backend.demo.simulate_reading --provider edge --realtime --timeline
```

Runs a scripted reading session — start, reading updates, Meaning Mode, OCR refinement, a stale frame, a paragraph advance, a page turn, session end — against the real `PlaybackEngine`, with the unfinished modules replaced by fakes (`FakeMergeMemory`, `FakeReadingEngine`). It ends with a nine-point checklist, one per timeline step, and exits non-zero if any behaviour is missing.

**Why this layer earns its place.** Unit tests assert on what you thought to assert on. Two defects got through a suite that was passing at the time:

- `start()` reset statistics unconditionally, so every page turn silently zeroed the reader's analytics. Every individual unit test passed; none of them turned a page mid-session and then looked at the tally.
- `LocalAudioSink` had no Windows player, so it discarded audio and returned instantly. Playback appeared instantaneous, the queue drained before any event could land, and Meaning Mode had nothing to interrupt — with the summary still reporting a full session.

The second one is the reason the checklist asserts `pause_count >= 1`. A run where nothing was ever interrupted is a run where the sink is lying.

**The realtime run is the one that matters.** Compressed time proves the sequencing; only real speech proves that Meaning Mode cuts in mid-word, that resume repeats the interrupted sentence rather than skipping it, and that an OCR refresh leaves the sentence in flight alone.

---

## Implementation Notes

1. **Lazy provider initialization** — the app must boot without Edge TTS credentials; the client is created on first `/audio/start` call.

2. **Sentence segmentation** — a regex in `sentence_queue.py`, not `nltk`. `nltk.sent_tokenize` needs a runtime corpus download, which is a poor thing to depend on mid-demo.

   A bare `.?!` split is not enough, because two cases are audibly wrong:

   - **Dialogue.** `"Stop!" she cried.` must stay one sentence. A lookahead requiring the next sentence to begin with a capital or digit handles it: the lowercase `she` shows the sentence is still running. Closing quotes are matched by a lookbehind rather than consumed as the separator, so `"Run!" "Why?"` keeps its quote marks.
   - **Abbreviations.** `Dr. Smith arrived.` must not pause after `Dr.` A small abbreviation set plus a single-letter rule (for initials like `J. K. Rowling`) rejoins those splits.

   Decimals (`$5.50`) and ellipses (`paused... then`) fall out of the same lookahead. This is not a full sentence tokenizer and doesn't need to be — but the dialogue case is not optional in a product built for reading novels aloud.

3. **Queue depth** — keep 3–5 sentences ahead. More wastes memory; fewer risks underflow if the Reading Engine is slow.

4. **Audio format** — Edge TTS returns MP3 by default. Stream it directly to the speaker; don't write to disk unless debugging.

5. **Error surfacing** — if synthesis fails, return a 200 with `status: "error"` and a reason. The Reading Engine decides whether to retry, fall back to offline, or surface the error to the user.

6. **Queue versioning** — `SentenceQueue` carries a monotonic `version`, bumped by every mutation. `dequeue()` deliberately does not bump it: consuming a sentence is not a rewrite of the queue, and a version that changed on every sentence would be useless for detecting one.

   Two separate counters, easily confused:

   - `queue_version` — the engine's own counter, exposed in `PlaybackStatus`. A client can tell whether the queue it last saw is still the queue in play.
   - `source_version` — Merge Memory's version of the *text*, supplied by the caller. The engine keeps the highest one applied and rejects anything at or below it.

   Rejection is not an error. `/audio/refresh-queue` returns 200 with `applied=false`, and `stale_updates_rejected` increments so a reordering problem upstream shows up in the summary instead of silently regressing the text.

7. **Statistics reset rule** — `start()` resets the tally only when the state is `IDLE`. Any other state means the session is still running and this `start()` is a page turn, so the tally carries over.

   A page turn passes through `IDLE`/`READY` transitions that zero the state machine's reading clock, so elapsed reading time is banked into a carried total before those transitions rather than read back afterwards.

   This is the bug the simulator found. The unit suite was green.

8. **Local playback needs a per-platform player** — `LocalAudioSink` tries `ffplay`, `mpv`, `afplay`, `aplay`, then falls back to PowerShell with WPF's `MediaPlayer`.

   The fallback exists because **Windows ships no command-line audio player**, and a sink that silently discards audio is worse than one that fails: playback appears instantaneous and the engine's timing behaviour never gets exercised. `MediaPlayer` is used rather than `SoundPlayer` (WAV only) or the WMPlayer COM object (needs a message pump) because it decodes MP3, reports real duration, and dies when the process is killed.

   `Open()` is asynchronous, so the script polls for `NaturalDuration` instead of trusting the first read, then sleeps for the media's actual length. This costs a process spawn per sentence (~200ms) — acceptable for a demo, but if local playback becomes the real path it wants one persistent player process.

9. **Structured logging, not a separate diagnostics system** — the engine emits single-line events through the standard module logger, configured by `backend/app/core/logging_config.py`:

   ```
   [audio:simulator] paused reason='meaning_mode' requeued='Her grandmother said the light went out '
   [audio:simulator] resumed next_sentence='Her grandmother said the light went out '
   [audio:simulator] queue_refresh_rejected reason='stale_source_version' received=1 current=1
   ```

   Every line carries the `session_id`, because sessions interleave in a shared log and a line without one is unattributable. Run the simulator with `--verbose` to see them.

---

## Dependencies

- `edge-tts>=6.1,<8.0` — Edge TTS client (verified against 7.2.8)
- `pyttsx3>=2.90,<3.0` — offline fallback (verified against 2.99)

Both are imported lazily, so the module loads without them and a provider only fails when you try to speak through it. `nltk` is deliberately **not** a dependency — see Implementation Note 2.

Local playback additionally needs a player on the host: `ffplay`, `mpv`, `afplay`, or `aplay`, with PowerShell as the Windows fallback. None is required to import the module, run the tests, or use the offline provider — see Implementation Note 8.

Add to `requirements.txt` and `.env.example`:

```env
# Audio Engine
EDGE_TTS_VOICE=en-US-AriaNeural
AUDIO_PROVIDER=edge  # "edge" or "offline"
```

---

## Frozen Interfaces

These are settled and should not change without a reason that survives argument. Downstream modules can build against them.

| Surface | Status |
| --- | --- |
| `ReadingPointer`, `PlaybackState`, `AudioProfile`, `SentenceChunk` | Frozen |
| `SpeechProviderInterface` (`synthesize`, `get_available_voices`, `provider_name`) | Frozen |
| `AudioSink` (`play`, `stop`) | Frozen |
| `PlaybackEngine` lifecycle (`start`, `pause`, `resume`, `stop`, `seek`) | Frozen |
| `AudioSessionManager` (`get`, `close`, `close_all`, `statuses`) | Frozen |
| `/audio` routes and `session_id` semantics | Frozen |
| `PlaybackStatistics` fields | Additive only — new counters may appear |
| `refresh_queue(source_version=...)` | Frozen |

`interfaces.py` declares the machine-readable form of the above and is checked against the implementations by `TestInterfaceConformance`, which compares parameter names and defaults for every Protocol method. Return annotations are deliberately not compared: returning the concrete `PlaybackEngine` where the Protocol declares `PlaybackEngineInterface` is correct covariance, not drift.

**Why that test exists.** These Protocols are what a downstream module codes against, and nothing executes them — a stale Protocol is indistinguishable from a correct one until integration fails. `refresh_queue` had already drifted (declared as returning `None`, with no `source_version`), and nothing caught it. The conformance test was verified by reintroducing exactly that drift and confirming it fails.

Add a Protocol method and its implementation in the same change, or the suite will tell you.
