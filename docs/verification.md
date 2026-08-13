# Verifying TaleTrace

Every command below is run from the repository root with the virtualenv active.
Nothing here needs the ESP32 rig except the last section.

## The one command

```bash
python -m scripts.verify_all "C:/Users/dell/OneDrive/Desktop/WhatsApp Image 2026-07-30 at 11.38.56 AM.jpeg"
```

Runs the unit suite, the nine-stage acceptance run on that photograph, and the
differential probe against `OCRandGESTURE/`, in that order, stopping at the first
failure and printing one summary. Exit status is 0 only if every stage passed.

Useful variants:

```bash
# No network at all. Run this first — a failure here needs no key to reproduce.
python -m scripts.verify_all page.jpg --offline

# Also probe the ESP32 rig. Fails on any machine not plugged into the device,
# which is why it is opt-in.
python -m scripts.verify_all page.jpg --hardware

# Do not stop at the first failure.
python -m scripts.verify_all page.jpg --keep-going
```

## The four stages on their own

### 1. Unit suite

```bash
python -m pytest -q                       # all 530
python -m pytest tests/test_runtime_e2e.py -q
python -m pytest -q -k "pointer or gesture"
```

Asserts that each module does what that module intends. Fast, offline, no keys.

### 2. Acceptance run — one photograph, nine stages

```bash
python -m scripts.validate_pipeline "path/to/page.jpg"
python -m scripts.validate_pipeline page.jpg --json .taletrace_cache/last_run.json
python -m scripts.validate_pipeline page.jpg --no-ai      # skip the key-1 call
python -m scripts.validate_pipeline page.jpg --no-groq    # skip both keys
python -m scripts.validate_pipeline page.jpg --fresh-ocr  # ignore the Vision cache
```

A real photograph of a finger pointing at a word, through the production chain:
Vision → parser → Merge Memory → Reading Engine → Gesture → word → sentence →
paragraph → Meaning Mode → AI Engine → Reading Speed → Audio → analytics. Each
stage prints what it produced and a pass/fail line.

Google Vision is called **once per image, ever**. The response is cached under
`.taletrace_cache/vision/`, keyed by the hash of the preprocessed bytes, and
every later stage replays it through the production parser. Groq is called at
most twice: once on `GROQ_API_KEY_2` for reconstruction, once on
`GROQ_API_KEY_1` for the explanation, and only after Meaning Mode begins.

### 3. Differential probe — migrated vs the reference

```bash
python -m scripts.equivalence_probe page.jpg              # everything
python -m scripts.equivalence_probe page.jpg --no-groq    # deterministic only, free
python -m scripts.equivalence_probe page.jpg --baseline   # show the LLM's ceiling
```

The same photograph through `OCRandGESTURE/` and through the migrated chain, with
both fed the *same* cached Vision response so no difference is the API's variance.

Deterministic and asserted — these must match exactly, and a mismatch sets the
exit status:

| comparison | what it covers |
|---|---|
| OCR rendering | Vision's flat text vs the page rebuilt from the word hierarchy |
| Gesture | word extraction, fingertip, selected word, line, paragraph, box, confidence, all three indices |
| Reading pointer | `calculate_accurate_pointer` vs `pointer_offset`, on three input pairs |
| Prompt context | every field the reference shows a reader reaches the AI prompt |

Reported, not asserted — a language model is involved:

| comparison | what it means |
|---|---|
| Page detection | same-page and different-page, both directions; a boolean, so still comparable |
| End to end | full reconstruction. **Cannot reach 100%** |

`--baseline` runs the reference against itself twice on the same photo. On the
sample page it agrees with itself ~81%, so an end-to-end figure near that is as
equivalent as the reference is to itself. Read the end-to-end number beside the
baseline, never on its own.

### 4. Hardware

```bash
python -m backend.app.live_session --check      # report what the rig can do, then exit
python -m backend.app.live_session              # run until Ctrl-C
python -m backend.app.live_session --seconds 60 # run for a minute
python -m backend.app.live_session -v           # with module logs
```

`--check` reports each credential and each endpoint separately, so "no camera" and
"no key" are different answers.

## Keys

Two separate Groq credentials, and they must not be mixed:

| variable | used by | why |
|---|---|---|
| `GROQ_API_KEY_1` | AI Engine — meaning, explanations, summaries | reader-triggered, once per request |
| `GROQ_API_KEY_2` | Merge Engine — reconstruction, same-page checks | camera loop, several times a second |
| `GOOGLE_VISION_API_KEY` | OCR | the one hard dependency |

Each subsystem builds its own client. A rate limit hit by the camera loop must not
stop a reader from asking what a word means.

## Costs at a glance

| command | Vision | Groq |
|---|---|---|
| `pytest -q` | 0 | 0 |
| `verify_all --offline` | 0 | 0 |
| `validate_pipeline` (cached image) | 0 | 2 |
| `equivalence_probe` | 0 | ~4 |
| `equivalence_probe --baseline` | 0 | ~6 |
| first run on a *new* image | 1 | as above |

Delete `.taletrace_cache/vision/` to force a fresh Vision call, or pass
`--fresh-ocr`.
