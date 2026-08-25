"""One real photograph, the whole production pipeline, every stage shown.

    python -m scripts.validate_pipeline "C:/path/to/page.jpg"
    python -m scripts.validate_pipeline page.jpg --no-ai       # skip the Groq_1 call
    python -m scripts.validate_pipeline page.jpg --fresh-ocr   # ignore the cache
    python -m scripts.validate_pipeline page.jpg --json out.json

The validation the unit suite cannot do. 500-odd tests assert that each module
does what that module intends; this asserts that a photograph of a person pointing
at a word in a book comes out the other end as an explanation of that word, with
the reading pointer, the audio queue and the analytics all describing the same
moment.

    photo ──► Vision (once) ──► parser ──► Merge Memory ──► Reading Engine
                  │                                            │
                  ▼ cached                                     ├─► Reading Speed
              replay for every                                 ├─► Audio Engine
              subsequent stage                                 ├─► AI Engine
                                                               └─► Reading Focus
    photo ──► finger detection ──► word selection ──► sentence ──► paragraph

Ten stages, each printed with what it produced, and each with a pass/fail line
at the end. Exit status is 0 only if every stage passed.

Cost
----
Google Vision is called **once** per image, ever: the response is cached under
`.taletrace_cache/vision/` keyed by the hash of the preprocessed bytes, and every
stage after OCR replays that cached response through the production parser. So
re-running this while debugging the Audio Engine costs nothing.

Groq is called at most twice, on two different keys:

    GROQ_API_KEY_2   once, Merge Memory reconstruction for the single frame
    GROQ_API_KEY_1   once, the explanation, and only after Meaning Mode begins

`--no-ai` drops the second, `--no-groq` drops both. Neither changes which code
path runs — only whether the network is reached.

What is real and what is not
----------------------------
Real: Google Vision (once, then its own cached answer), the finger detector, the
word selector, Merge Memory and its Groq reconstruction, the Reading Engine, the
Reading Speed service, the Audio Engine's state machine and queue, the AI Engine,
the Reading Focus Analysis engine.

Not real: speech *synthesis*. The Audio Engine runs against a provider that takes
time but produces no sound, and a null sink, because this validates that the right
sentence was queued at the right pointer, not that a speaker made a noise. It is
paced rather than instant so that narration is genuinely in flight when the reader
gestures — an instant provider drains the queue first and the interruption under
test never happens. Everything the engine decides is the production code, at the
production `auto_advance`; only the audio device is absent.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.core.environment import load_environment  # noqa: E402
from backend.app.modules.ai_engine import engines as ai_engines  # noqa: E402
from backend.app.modules.ai_engine.models import BookMetadata  # noqa: E402
from backend.app.modules.audio_engine.models import (  # noqa: E402
    PauseReason,
    PlaybackState,
    SpeechRequest,
    SpeechResponse,
    Voice,
)
from backend.app.modules.audio_engine.playback_engine import PlaybackEngine  # noqa: E402
from backend.app.modules.audio_engine.sentence_queue import segment_sentences  # noqa: E402
from backend.app.modules.audio_engine.speech_provider import NullAudioSink  # noqa: E402
from backend.app.modules.gesture_engine.detector import detect_finger  # noqa: E402
from backend.app.modules.gesture_engine.selection_models import (  # noqa: E402
    SelectionConfig,
    SelectionStatus,
)
from backend.app.modules.merge_memory.engine import MergeMemory  # noqa: E402
from backend.app.modules.merge_memory.reconstruction import GroqReconstructor  # noqa: E402
from backend.app.modules.ocr.providers import GoogleVisionProvider  # noqa: E402
from backend.app.modules.ocr.replay import ReplayAdapter  # noqa: E402
from backend.app.modules.reading_engine.ai_bridge import AiBridge  # noqa: E402
from backend.app.modules.reading_engine.runtime import ReadingRuntime  # noqa: E402
from backend.app.modules.reading_speed.models import DifficultyLevel  # noqa: E402
from backend.app.modules.reading_speed.service import ReadingSpeedService  # noqa: E402
from backend.app.shared.groq_keys import ai_engine_key, merge_engine_key  # noqa: E402
from scripts import vision_cache  # noqa: E402

SESSION_ID = "validation-session"
READER_ID = "validation-reader"


# ----------------------------------------------------------------- reporting


@dataclass
class Stage:
    """One verification stage: what it is called, whether it held, and why."""

    number: int
    name: str
    passed: bool = False
    detail: str = ""
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    def check(self, label: str, ok: bool, detail: str = "") -> bool:
        """Record one assertion. Returns `ok` so callers can branch on it."""

        self.checks.append((label, bool(ok), detail))
        return bool(ok)

    def settle(self) -> None:
        self.passed = bool(self.checks) and all(ok for _, ok, _ in self.checks)


class Report:
    """Accumulates stages and prints them. The only thing that writes to stdout."""

    def __init__(self) -> None:
        self.stages: list[Stage] = []
        self.notes: list[str] = []

    def stage(self, number: int, name: str) -> Stage:
        stage = Stage(number=number, name=name)
        self.stages.append(stage)
        print(f"\n{'=' * 74}")
        print(f"  STAGE {number} — {name}")
        print("=" * 74)
        return stage

    def field(self, label: str, value: Any, width: int = 22) -> None:
        print(f"  {str(label).ljust(width)}  {value}")

    def block(self, label: str, text: str, *, limit: int = 700) -> None:
        body = (text or "").strip()
        if not body:
            print(f"  {label}: (empty)")
            return
        shown = body if len(body) <= limit else body[:limit] + f" … [+{len(body) - limit} chars]"
        print(f"\n  {label}:")
        for line in shown.splitlines():
            print(f"    {line}")

    def note(self, message: str) -> None:
        self.notes.append(message)
        print(f"  note: {message}")

    def close(self, stage: Stage) -> None:
        stage.settle()
        print()
        for label, ok, detail in stage.checks:
            # The detail is why the check *would* have failed, so it is printed
            # only when it did. Shown on a passing line it reads as a
            # contradiction — "[PASS] the sentence was requeued — it was dropped".
            mark = "PASS" if ok else "FAIL"
            print(f"  [{mark}] {label}" + ("" if ok else f" — {detail}" if detail else ""))

    def summary(self) -> bool:
        print(f"\n{'=' * 74}")
        print("  ACCEPTANCE SUMMARY")
        print("=" * 74)
        width = max(len(s.name) for s in self.stages) if self.stages else 10
        for stage in self.stages:
            mark = "PASS" if stage.passed else "FAIL"
            print(f"  {mark}  {stage.number:>2}. {stage.name.ljust(width)}  {stage.detail}")

        failed = [s for s in self.stages if not s.passed]
        print()
        if failed:
            print(f"  {len(failed)} of {len(self.stages)} stages failed:")
            for stage in failed:
                for label, ok, detail in stage.checks:
                    if not ok:
                        print(f"    stage {stage.number}: {label}" + (f" — {detail}" if detail else ""))
        else:
            print(f"  all {len(self.stages)} stages passed")
        return not failed


# ------------------------------------------------------------------ the OCR cache


def vision_once(image_path: Path, *, fresh: bool, report: Report) -> tuple[dict, bytes, bool]:
    """This image's Vision response, from the cache the dev scripts share.

    Thin on purpose. The cache lives in `scripts.vision_cache` because the
    differential probe reads the same entries: if the two harnesses kept separate
    caches, the probe would compare the reference against a Vision response this
    run never saw, and a difference between them could not be attributed.
    """

    return vision_cache.fetch_once(image_path, fresh=fresh, note=report.note)


# --------------------------------------------------------- the prompt recorder


class PromptRecorder:
    """Captures the literal strings the AI Engine sends, and how many times.

    Wraps `engines._safe_json_completion` rather than rebuilding the prompt from
    a `ReadingContext`: a reconstruction could agree with the code that builds it
    and still not be what went over the wire. This is what went over the wire.

    Also the enforcement point for "the AI is called once": every call is counted,
    so a second one is visible rather than merely unlikely.
    """

    def __init__(self, *, live: bool) -> None:
        self.calls: list[tuple[str, str]] = []
        self._live = live
        self._original = ai_engines._safe_json_completion

    def __enter__(self) -> PromptRecorder:
        def recording(system_prompt: str, user_content: str, max_retries: int = 2) -> dict:
            self.calls.append((system_prompt, user_content))
            if not self._live:
                return {"error": "AI call suppressed by --no-ai"}
            return self._original(system_prompt, user_content, max_retries)

        ai_engines._safe_json_completion = recording  # type: ignore[assignment]
        # The module caches its client, and `.env` was loaded after import in
        # some entry points. Dropping it forces the key to be re-read.
        ai_engines.reset_client()
        return self

    def __exit__(self, *exc: Any) -> None:
        ai_engines._safe_json_completion = self._original  # type: ignore[assignment]


class PacedSpeechProvider:
    """`FakeSpeechProvider` that takes time, because narration does.

    An instant provider is the wrong model of the Audio Engine for this harness.
    Two of its central behaviours only exist while a sentence is in flight: a
    pointer move mid-sentence is *deferred* to the next sentence boundary, and
    Meaning Mode is the one interruption allowed to cut a sentence off. Against a
    provider that returns immediately the queue is drained before the gesture
    arrives, both behaviours are unobservable, and stage 8 would be reporting on
    a finished session rather than a reader mid-page.

    So each sentence costs a fixed slice of real time. Not synthesis, and no
    audio device — the same `FakeSpeechProvider` contract with a delay, which is
    the one property of a real provider the engine's sequencing depends on.
    """

    provider_name = "paced-fake"

    def __init__(self, *, seconds_per_sentence: float) -> None:
        self._delay = seconds_per_sentence
        self.spoken: list[str] = []
        self._speaking = asyncio.Event()

    async def synthesize(self, request: SpeechRequest) -> SpeechResponse:
        # Recorded before the delay, so `spoken` means "sentences begun". An
        # interrupted sentence is a sentence the reader heard part of, and
        # `pause()` requeues it — counting it only on completion would lose it.
        self.spoken.append(request.text)
        self._speaking.set()
        await asyncio.sleep(self._delay)
        return SpeechResponse(audio=b"paced-audio", provider=self.provider_name)

    async def get_available_voices(self) -> list[Voice]:
        return [Voice(id="paced-voice", name="Paced Fake", locale="en-US")]

    def forget_speaking(self) -> None:
        """Arm the latch so the next `wait_until_speaking` is about the next sentence."""

        self._speaking.clear()

    async def wait_until_speaking(self, *, timeout: float = 5.0) -> bool:
        """Block until a sentence is genuinely in flight. False if none started.

        This is what removes the race from the Meaning Mode check. Without it the
        harness would gesture at whatever moment it happened to reach and hope
        narration had not already finished — and on a page whose paragraph is one
        sentence long, that hope is thin.
        """

        try:
            await asyncio.wait_for(self._speaking.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False


# ----------------------------------------------------------------- the run


async def run(args: argparse.Namespace) -> int:
    report = Report()
    image_path = Path(args.image).expanduser()
    if not image_path.is_file():
        raise SystemExit(f"Not a file: {image_path}")

    live_merge = bool(merge_engine_key()) and not args.no_groq
    live_ai = bool(ai_engine_key()) and not args.no_groq and not args.no_ai

    print("TaleTrace — end-to-end production validation")
    report.field("image", f"{image_path.name}  ({image_path.stat().st_size:,} bytes)")
    report.field("Vision", "live once, then cached" if not args.fresh_ocr else "forced fresh")
    report.field("Merge Engine (key 2)", "live Groq" if live_merge else "disabled — raw OCR")
    report.field("AI Engine (key 1)", "live Groq" if live_ai else "disabled — no explanation")
    report.field("Audio", "real engine, fake speech provider")

    # ---------------------------------------------------------------- stage 1
    stage = report.stage(1, "Google Vision OCR")

    response, prepared, cached = vision_once(image_path, fresh=args.fresh_ocr, report=report)
    words = tuple(GoogleVisionProvider.parse_response(response))

    report.field("source", "cache (0 API calls)" if cached else "Google Vision (1 API call)")
    report.field("words detected", len(words))
    report.field("lines", len({w.line_index for w in words}))
    report.field("paragraphs", len({w.paragraph_index for w in words}))
    boxed = [w for w in words if w.bbox != (0, 0, 0, 0)]
    report.field("words with a box", f"{len(boxed)} / {len(words)}")
    if words:
        report.field("first five", " ".join(w.text for w in words[:5]))
        report.field("last five", " ".join(w.text for w in words[-5:]))

    stage.check("Vision returned words", bool(words), f"{len(words)} words")
    stage.check(
        "every word has a bounding box",
        len(boxed) == len(words),
        f"{len(words) - len(boxed)} without geometry",
    )
    stage.check(
        "reading order is assigned",
        all(w.word_index >= 0 for w in words),
        "some words carry word_index -1",
    )
    stage.check(
        "reading order is monotonic",
        [w.word_index for w in words] == sorted(w.word_index for w in words),
        "word_index is not increasing in document order",
    )
    stage.detail = f"{len(words)} words, {len({w.line_index for w in words})} lines"
    report.close(stage)

    # The rest of the run replays this response. One network call, one parse, and
    # every downstream stage sees exactly the words printed above.
    reconstructor = GroqReconstructor(api_key=merge_engine_key() if live_merge else "")
    memory = MergeMemory(reconstruct=reconstructor)
    speed = ReadingSpeedService()
    # Paced rather than instant, and `auto_advance=True` rather than stepped:
    # both so this runs the Audio Engine the way production does. An instant
    # provider drains the queue before any gesture arrives, and `auto_advance=False`
    # retires the loop after one sentence — between them the two would make every
    # stage-8 assertion a statement about a finished session instead of a reader
    # mid-page, and would hide the pointer handling they exist to check.
    speech = PacedSpeechProvider(seconds_per_sentence=args.sentence_seconds)
    audio = PlaybackEngine(
        provider=speech,
        sink=NullAudioSink(),
        session_id=SESSION_ID,
    )
    ai = AiBridge(
        book=BookMetadata(title=args.book_title or None, audience=args.audience or None),
        timeout=args.ai_timeout,
    )

    runtime = ReadingRuntime.build(
        session_id=SESSION_ID,
        reader_id=READER_ID,
        ocr_provider=ReplayAdapter(),
        memory=memory,
        speed=speed,
        audio=audio,
        ai=ai,
        confirm_page_change=reconstructor.is_same_page,
    )
    engine = runtime.engine

    # The frame goes in *before* the session starts, which is the real order: the
    # camera streams as soon as the book is in view, and those early frames are
    # what give the session text to open with. `_apply_text` has an explicit
    # pre-session branch for exactly this. Reversed, `start_session` would hand
    # the Audio Engine an empty paragraph, the queue would be empty, playback
    # would reach FINISHED immediately, and every later pause would be a no-op —
    # so stage 8 would be measuring a dead engine.
    frame = await runtime.feed_camera_frame(response)
    await engine.start_session()
    # Let the opening paragraph settle before the reader gestures. On this page
    # OCR's first paragraph is a one-word fragment, so narration drains it and the
    # engine reaches FINISHED — which is exactly the state stage 2's pointer move
    # has to revive playback out of.
    await audio.wait_for_idle(timeout=args.sentence_seconds * 4 + 5.0)
    speech.forget_speaking()

    # ---------------------------------------------------------------- stage 2
    stage = report.stage(2, "Gesture — the pointed-at word")

    image = decode_bgr(image_path.read_bytes())
    if image is None:
        raise SystemExit("OpenCV could not decode the image; gesture detection cannot run.")

    config = SelectionConfig()
    finger = detect_finger(image, config)
    report.field("frame size", f"{image.shape[1]} x {image.shape[0]}")
    if finger is None:
        report.field("finger", "not detected")
    else:
        report.field("finger", f"({finger.x:.0f}, {finger.y:.0f})")
        report.field("detection method", finger.detection_method)
        report.field("finger confidence", f"{finger.confidence:.2f}")
        report.field("pointing direction", finger.direction)

    stage.check("a fingertip was found in the photograph", finger is not None)

    selection = await runtime.feed_gesture_frame(image, finger=finger)
    report.field("status", selection.status.value)
    report.field("selected word", repr(selection.selected_word))
    report.field("selection confidence", f"{selection.confidence:.3f}")
    report.field("word bbox", selection.selected_word_bbox)
    report.field("word / line / para", f"{selection.word_index} / {selection.line_index} / {selection.paragraph_index}")
    report.field("selection took", f"{selection.selection_time_ms:.1f} ms")
    report.block("reason", selection.selection_reason)
    if selection.candidate_scores:
        print("\n  candidates considered:")
        for candidate in selection.candidate_scores:
            print(f"    {candidate.total_score:.3f}  {candidate.word.text!r}")

    stage.check(
        "a word was resolved",
        selection.status is SelectionStatus.SUCCESS,
        f"status {selection.status.value}",
    )
    stage.check("the word is non-empty", bool(selection.selected_word.strip()))
    stage.check(
        "the word carries its own box",
        selection.selected_word_bbox is not None
        and selection.selected_word_bbox != (0, 0, 0, 0),
    )
    stage.check(
        "confidence is above the selection threshold",
        selection.confidence >= config.confidence_threshold,
        f"{selection.confidence:.3f} < {config.confidence_threshold}",
    )
    stage.check(
        "the selected word is one Vision actually read",
        any(w.text == selection.selected_word for w in words),
        "the selector invented a word",
    )
    stage.check(
        "gesture triggered no further OCR",
        frame.version == engine.ocr.version,
        f"OCR version moved {frame.version} -> {engine.ocr.version}",
    )
    stage.detail = f"{selection.selected_word!r} @ {selection.confidence:.2f}"
    report.close(stage)

    # ---------------------------------------------------------------- stage 3
    stage = report.stage(3, "Sentence containing the word")

    sentence = selection.context
    report.block("sentence", sentence)
    report.field("words in sentence", len(sentence.split()))

    bare = selection.selected_word.strip(".,;:!?\"'“”‘’()[]")
    stage.check("a sentence was extracted", bool(sentence.strip()))
    stage.check(
        "the sentence contains the selected word",
        bare.lower() in sentence.lower(),
        f"{bare!r} is not in the extracted sentence",
    )
    stage.check(
        "the sentence is more than the word alone",
        len(sentence.split()) > 1,
        "extraction returned a single token",
    )
    stage.detail = f"{len(sentence.split())} words"
    report.close(stage)

    # ---------------------------------------------------------------- stage 4
    stage = report.stage(4, "Paragraph containing the sentence")

    paragraph = selection.selected_paragraph
    report.block("paragraph", paragraph)
    report.field("words in paragraph", len(paragraph.split()))

    stage.check("a paragraph was extracted", bool(paragraph.strip()))
    stage.check(
        "the paragraph contains the sentence",
        _contains_words(paragraph, sentence),
        "the sentence's words are not all in the paragraph",
    )
    stage.check(
        "the paragraph is at least the sentence",
        len(paragraph.split()) >= len(sentence.split()),
    )
    stage.detail = f"{len(paragraph.split())} words"
    report.close(stage)

    # ---------------------------------------------------------------- stage 5
    stage = report.stage(5, "Merge Memory and Meaning Mode entry")

    report.field("merge memory version", memory.version)
    report.field("current page", memory.current_page)
    report.field("paragraphs held", memory.paragraph_count(memory.current_page))
    report.field("words held", memory.total_words)
    report.field("reconstruction", "Groq (key 2)" if live_merge else "disabled")
    report.block("page text", memory.page_text(memory.current_page))

    stage.check("Merge Memory holds the page", memory.total_words > 0)
    stage.check(
        "Merge Memory advanced exactly one version for one frame",
        memory.version == 1,
        f"version is {memory.version}",
    )
    if live_merge:
        # The no-key fallback returns the raw text unchanged (`"" + "\n" + new`,
        # stripped), so "the held text differs from what OCR handed over" is the
        # one observable that separates a real reconstruction from the fallback.
        stage.check(
            "reconstruction changed the raw OCR text",
            memory.page_text(memory.current_page).split() != frame.text.split(),
            "held text is identical to raw OCR — the fallback path ran",
        )

    # Meaning Mode is entered the way the hardware enters it: the gesture frame
    # is re-observed with the meaning flag set, which publishes MEANING_REQUESTED
    # and lets the Reading Engine decide. Nothing here calls `meaning_mode_on`.
    with PromptRecorder(live=live_ai) as recorder:
        # Wait until a sentence is actually being spoken before interrupting it.
        # Stage 2's pointer move revived playback on the paragraph the reader
        # pointed into; gesturing before narration reaches it would test pausing
        # an idle engine, which is not what a reader does.
        speaking = await speech.wait_until_speaking(timeout=args.sentence_seconds + 2.0)
        if not speaking:
            report.note("no sentence was in flight when Meaning Mode was requested")

        runtime.gesture.reset()
        # Stamped either side of the lookup so stage 10 can check that these
        # seconds were kept out of reading time. Wall time rather than either
        # engine's clock: a clock that wrongly kept running through Meaning Mode
        # cannot be used to measure how long Meaning Mode lasted.
        meaning_started_at = time.monotonic()
        await runtime.feed_gesture_frame(image, finger=finger, meaning_gesture=True)

        report.field("meaning mode", engine.state.is_meaning_mode)
        report.field("pointer", engine.state.pointer.sentence_order_key())
        report.field("AI calls made", len(recorder.calls))

        stage.check(
            "Meaning Mode was entered by the published gesture event",
            engine.state.is_meaning_mode,
            "MEANING_REQUESTED did not reach the engine",
        )
        stage.check(
            "the AI Engine was asked exactly once",
            len(recorder.calls) == 1,
            f"{len(recorder.calls)} calls",
        )

        if recorder.calls:
            system_prompt, user_content = recorder.calls[0]
            report.block("AI system prompt", system_prompt, limit=1600)
            report.block("AI user content", user_content, limit=1600)

            stage.check("the prompt names the selected word", bare in user_content)
            stage.check(
                "the prompt carries the current paragraph",
                "Current paragraph:" in user_content,
            )
            stage.check(
                "the prompt carries the page number",
                f"Page: {engine.state.pointer.page_index}" in user_content,
            )
            stage.check(
                "the prompt carries the reading mode",
                "Reading mode:" in user_content,
            )
            prompt_paragraph = _prompt_paragraph(user_content)
            held = [
                memory.paragraph(memory.current_page, index)
                for index in range(memory.paragraph_count(memory.current_page))
            ]
            stage.check(
                "the paragraph in the prompt is Merge Memory's, not raw OCR",
                prompt_paragraph in held,
                "the AI was sent text Merge Memory does not hold",
            )
            # Not the pointer's paragraph. Meaning Mode does not move the pointer,
            # so on a page with more than one paragraph the reader can point into
            # the third while the pointer is still in the first — and comparing
            # the prompt against the pointer would agree with itself while the
            # model was being asked about a word from somewhere else entirely.
            stage.check(
                "the paragraph in the prompt is the one the word was pointed at in",
                bare.lower() in prompt_paragraph.lower(),
                "the AI was asked about a word alongside a paragraph it does not "
                "appear in",
            )
            stage.check(
                "the prompt names no hardcoded book",
                "Septopus" not in system_prompt,
                "a specific book title leaked into a book-agnostic prompt",
            )
    stage.detail = f"v{memory.version}, meaning mode on"
    report.close(stage)

    # ---------------------------------------------------------------- stage 6
    stage = report.stage(6, "AI Engine response")

    explanation = engine.explanation
    if explanation is None:
        report.field("explanation", "none — no AI engine attached")
    else:
        report.field("ok", explanation.ok)
        report.field("capability", explanation.capability)
        if explanation.error:
            report.field("error", explanation.error)
        report.block("OLED text", explanation.oled_text)
        report.block("full explanation", explanation.explanation)
        for key in ("difficulty_level", "contextual_meaning", "examples", "reading_insight"):
            if key in explanation.data:
                report.block(key, str(explanation.data[key]))

    if live_ai:
        stage.check("an explanation came back", explanation is not None and explanation.ok,
                    explanation.error if explanation else "no call made")
        if explanation is not None and explanation.ok:
            stage.check("the OLED line is short enough for the display",
                        0 < len(explanation.oled_text.split()) <= 14,
                        f"{len(explanation.oled_text.split())} words")
            stage.check("the explanation is a readable sentence, not a stub",
                        len(explanation.explanation.split()) >= 8,
                        f"{len(explanation.explanation.split())} words")
            stage.check("a difficulty level was reported",
                        bool(explanation.data.get("difficulty_level")))
            stage.check("the lookup was recorded for the session review",
                        bare.lower() in " ".join(engine.lookups).lower(),
                        f"lookups: {engine.lookups}")
    else:
        stage.check(
            "AI disabled: the session survived a failed lookup",
            explanation is not None and not explanation.ok and engine.state.is_meaning_mode,
            "a failed explanation should still leave Meaning Mode on",
        )
    stage.detail = "explained" if (explanation and explanation.ok) else "no explanation"
    report.close(stage)

    # ---------------------------------------------------------------- stage 7
    stage = report.stage(7, "Reading Speed")

    prediction = speed.predict(SESSION_ID)
    snapshot = speed.tracker(SESSION_ID).snapshot()
    report.field("baseline wpm", f"{prediction.baseline_wpm:.1f}")
    report.field("expected word offset", prediction.expected_word_offset)
    report.field("actual word offset", prediction.actual_word_offset)
    report.field("expected sentence", prediction.expected_sentence_index)
    report.field("actual sentence", prediction.actual_sentence_index)
    report.field("expected finish (ms)", prediction.expected_finish_ms)
    report.field("progress %", f"{prediction.progress_percentage:.2f}")
    report.field("expected progress %", f"{prediction.expected_progress_percentage:.2f}")
    report.field("deviation words", prediction.deviation_words)
    report.field("deviation ms", prediction.deviation_ms)
    report.field("prediction confidence", f"{prediction.confidence:.2f}")
    report.field("reading clock (ms)", snapshot.elapsed_reading_ms)
    report.field("wall clock (ms)", snapshot.elapsed_wall_ms)
    report.field("mode", snapshot.mode.value)
    report.field("lookups", snapshot.lookup_count)
    report.field("meaning mode entries", snapshot.meaning_mode_count)

    difficulties = speed.page_difficulties(SESSION_ID)
    for metrics in difficulties:
        report.field(
            f"page {metrics.page_index}",
            f"{metrics.words} words, expected {metrics.expected_ms}ms, "
            f"actual {metrics.actual_ms}ms, {metrics.difficulty.value}",
        )

    stage.check(
        "the content map describes the page Merge Memory holds",
        engine.content.total_words == memory.total_words,
        f"speed sees {engine.content.total_words} words, memory holds {memory.total_words}",
    )
    stage.check(
        "the reading clock is stopped in Meaning Mode",
        not snapshot.is_clock_running,
        f"mode is {snapshot.mode.value} while Meaning Mode is on",
    )
    stage.check(
        "the wall clock is at least the reading clock",
        snapshot.elapsed_wall_ms >= snapshot.elapsed_reading_ms,
    )
    stage.check(
        "Meaning Mode was counted",
        snapshot.meaning_mode_count >= 1,
        f"{snapshot.meaning_mode_count} entries recorded",
    )
    stage.check(
        "expected and actual positions are both measured, not invented",
        prediction.expected_word_offset >= 0 and prediction.actual_word_offset >= 0,
    )
    stage.check(
        "progress is bounded",
        0.0 <= prediction.progress_percentage <= 100.0
        and 0.0 <= prediction.expected_progress_percentage <= 100.0,
    )
    stage.check(
        "a pace baseline exists to predict against",
        prediction.baseline_wpm > 0,
        "no baseline — prediction would be meaningless",
    )
    stage.detail = f"{prediction.progress_percentage:.1f}% read, conf {prediction.confidence:.2f}"
    report.close(stage)

    # ---------------------------------------------------------------- stage 8
    stage = report.stage(8, "Audio Engine synchronisation")

    status = audio.get_status()
    report.field("state", status.state.value)
    report.field("pause reason", status.pause_reason.value if status.pause_reason else None)
    report.field("audio pointer", status.pointer.sentence_order_key() if status.pointer else None)
    report.field("engine pointer", engine.state.pointer.sentence_order_key())
    report.field("queued sentences", status.queued_sentences)
    report.field("queue version", status.queue_version)
    report.field("current sentence", repr(status.current_sentence))
    report.field("provider", status.provider)
    report.field("sentences spoken", len(speech.spoken))

    expected_queue = segment_sentences(
        engine.current_text(),
        start_pointer=engine.state.pointer.at_paragraph_start(),
    )
    report.field("sentences in paragraph", len(expected_queue))

    # Narration is checked against the whole page, not against the paragraph the
    # pointer currently names. The reader pointed at a word in a later paragraph
    # mid-session, so by now the engine holds *that* paragraph while the sentences
    # already spoken came from the one the session opened on. Both are Merge
    # Memory's text and neither is drift; comparing spoken lines to the current
    # paragraph alone would fail the harness for the pointer having moved, which
    # is the behaviour under test.
    page_text = memory.page_text(memory.current_page)
    spoken_from_page = [line for line in speech.spoken if _contains_words(page_text, line)]
    narrated_paragraphs = _narrated_paragraphs(memory, memory.current_page, speech.spoken)
    report.field("spoken from this page", f"{len(spoken_from_page)} / {len(speech.spoken)}")
    report.field("narrated paragraphs", sorted(narrated_paragraphs))

    # Both pointers name a sentence of the same paragraph on the same page. They
    # are not required to be the same sentence: a pointer move mid-sentence is
    # deliberately deferred until narration finishes it, so audio legitimately
    # trails the engine by one. Requiring equality would fail the harness for the
    # behaviour the Audio Engine exists to provide.
    stage.check(
        "the audio pointer and the engine pointer are on the same page",
        status.pointer is not None
        and status.pointer.page_index == engine.state.pointer.page_index,
        "the two disagree about which page is being read",
    )
    stage.check(
        "the audio pointer is in a paragraph Merge Memory holds",
        status.pointer is not None
        and 0 <= status.pointer.paragraph_index < memory.paragraph_count(memory.current_page),
        "playback is pointing outside the page",
    )
    stage.check(
        "the audio pointer reached the paragraph the reader pointed into",
        status.pointer is not None
        and status.pointer.paragraph_index == engine.state.pointer.paragraph_index,
        f"audio is on paragraph {status.pointer.paragraph_index if status.pointer else None}, "
        f"the engine on {engine.state.pointer.paragraph_index}",
    )
    stage.check(
        "playback is paused for Meaning Mode, not for the reader",
        status.pause_reason is PauseReason.MEANING_MODE,
        f"pause reason is {status.pause_reason}",
    )
    stage.check(
        "playback actually stopped for the lookup",
        status.state is PlaybackState.PAUSED,
        f"state is {status.state.value}",
    )
    stage.check(
        "the queue was built from Merge Memory's paragraph",
        0 < status.queued_sentences <= len(expected_queue),
        f"{status.queued_sentences} queued, paragraph holds {len(expected_queue)}",
    )
    stage.check(
        "the interrupted sentence was requeued, not lost",
        status.queued_sentences >= 1,
        "Meaning Mode cut a sentence off and dropped it",
    )
    stage.check(
        "nothing was narrated that Merge Memory does not hold",
        len(spoken_from_page) == len(speech.spoken),
        f"{len(speech.spoken) - len(spoken_from_page)} spoken lines are not on this page",
    )
    stage.check(
        "narration stayed within the paragraphs the reader visited",
        narrated_paragraphs <= {0, engine.state.pointer.paragraph_index},
        f"also narrated {sorted(narrated_paragraphs - {0, engine.state.pointer.paragraph_index})}",
    )
    stage.check(
        "narration did not run past the pointer",
        status.queued_sentences >= len(expected_queue) - len(speech.spoken),
        f"{len(speech.spoken)} spoken but {status.queued_sentences} still queued",
    )
    stage.check(
        "playback survived the page running dry mid-session",
        len(speech.spoken) > 1,
        "only one sentence was ever spoken — the queue was never revived",
    )
    stage.detail = f"{status.state.value}, {status.queued_sentences} queued"
    report.close(stage)

    # ---------------------------------------------------------------- stage 9
    stage = report.stage(9, "Session analytics")

    await engine.meaning_mode_off()
    meaning_mode_ms = int((time.monotonic() - meaning_started_at) * 1000)
    analytics = await engine.finish_session(review=False)

    report.field("session", analytics.session_id)
    report.field("reader", analytics.reader_id)
    report.field("pages read", analytics.pages_read)
    report.field("words read", analytics.words_read)
    report.field("baseline wpm", f"{analytics.baseline_wpm:.1f}")
    report.field("session wpm", f"{analytics.session_wpm:.1f}")
    report.field("reading duration ms", analytics.reading_duration_ms)
    report.field("wall duration ms", analytics.wall_duration_ms)
    report.field("lookups", analytics.lookup_count)
    report.field("meaning requests", analytics.meaning_requests)
    report.field("hardest page", analytics.hardest_page)
    report.field("tts assisted", analytics.tts_assisted)
    report.field("selected word", repr(selection.selected_word))
    report.field("merge memory version", memory.version)
    report.field("final pointer", engine.state.pointer.sentence_order_key())
    report.field("content version", engine.state.content_version)

    print("\n  event sequence:")
    for event in engine.events:
        print(f"    {event.event.value}" + (f"  {event.detail}" if event.detail else ""))

    stage.check("the page the reader read is in history", analytics.pages_read >= 1)
    stage.check(
        "words read is consistent with the page held",
        0 < analytics.words_read <= memory.total_words,
        f"{analytics.words_read} read vs {memory.total_words} held",
    )
    stage.check(
        "the lookup count matches the lookups recorded",
        analytics.lookup_count == len(engine.lookups),
        f"analytics {analytics.lookup_count} vs engine {len(engine.lookups)}",
    )
    stage.check(
        "Meaning Mode is reflected in the analytics",
        analytics.meaning_requests >= 1,
        f"{analytics.meaning_requests} recorded",
    )
    stage.check(
        "the state's content version is Merge Memory's version",
        engine.state.content_version == memory.version,
        f"state v{engine.state.content_version} vs memory v{memory.version}",
    )
    stage.check("the session is finished and not paused",
                engine.state.is_finished and not engine.state.is_paused)
    stage.check("Meaning Mode was left before the session ended",
                not engine.state.is_meaning_mode)
    stage.check(
        "wall time is at least reading time",
        analytics.wall_duration_ms >= analytics.reading_duration_ms,
    )
    stage.detail = f"{analytics.pages_read} page, {analytics.words_read} words"
    report.close(stage)

    # ---------------------------------------------------------------- stage 10
    stage = report.stage(10, "Reading Focus Analysis")

    # Last, because the report does not exist until the session ends: the final
    # paragraph's idle time is only revealed by `session_finished`, and the
    # baseline the paragraphs are judged against is the one the session finished
    # with. Asking mid-session would be reading a different report.
    focus = engine.focus_report
    baseline_before = speed.baseline_for(READER_ID)

    stage.check(
        "the session produced a focus analysis",
        focus is not None,
        "no focus engine was wired into the runtime",
    )

    if focus is None:
        stage.detail = "no report"
        report.close(stage)
    else:
        report.field("session / reader", f"{focus.session_id} / {focus.reader_id}")
        report.field("baseline wpm", f"{focus.baseline_wpm:.1f}")
        report.field("baseline is evidence", focus.baseline_was_evidence)
        report.field("paragraphs analysed", focus.paragraphs_analysed)
        report.field("words analysed", f"{focus.total_words} of {memory.total_words} held")
        report.field("focused ms", focus.total_focused_ms)
        report.field("possible idle ms", focus.total_idle_ms)
        report.field("meaning mode lasted", f"{meaning_mode_ms} ms (wall)")

        print("\n  per paragraph:")
        for paragraph_focus in focus.paragraphs:
            print(
                f"    p{paragraph_focus.page_index}¶{paragraph_focus.paragraph_index}  "
                f"{paragraph_focus.words:>4} words  "
                f"expected {paragraph_focus.expected_ms:>6}ms  "
                f"actual {paragraph_focus.actual_ms:>6}ms  "
                f"idle {paragraph_focus.idle_ms:>6}ms  "
                f"{paragraph_focus.difficulty.value:<7}  "
                f"priority {paragraph_focus.revision_priority:>5.1f}"
            )
            for line in paragraph_focus.evidence:
                print(f"        - {line}")

        attention = focus.needs_attention()
        report.field(
            "needs attention",
            ", ".join(f"¶{p.paragraph_index} ({p.revision_priority:.1f})" for p in attention)
            or "nothing scored above zero",
        )

        if not focus.baseline_was_evidence:
            # Said out loud so that a report of all-UNKNOWN is not read as the
            # engine failing to work. The validation reader has never been
            # calibrated, and a paragraph judged against a default 200 wpm would
            # be a verdict about an assumption. Manufacturing a calibration here
            # to make the ranking non-empty would be inventing a reader — the
            # difficulty branches are exercised by the unit suite, which can hand
            # the engine a measured baseline honestly.
            report.note(
                "the baseline is a default, not a measurement, so every paragraph is "
                "UNKNOWN — correct for a reader's first session"
            )
        if meaning_mode_ms == 0:
            report.note(
                "Meaning Mode lasted under a millisecond, so the 'lookup time is not "
                "reading time' check is not stressed; run without --no-ai to stress it"
            )

        held_paragraphs = {
            (memory.current_page, index)
            for index in range(memory.paragraph_count(memory.current_page))
        }
        analysed = {p.key for p in focus.paragraphs}

        stage.check(
            "every paragraph analysed is one Merge Memory holds",
            analysed <= held_paragraphs,
            f"analysed {sorted(analysed - held_paragraphs)}, which the page does not contain",
        )
        # The "never reconstructs text" rule, checked rather than trusted. The
        # engine gets its word counts from Merge Memory's content map; if it ever
        # started splitting strings itself, this is where the two would part.
        stage.check(
            "word counts are Merge Memory's, not counted here",
            all(
                p.words == len(memory.paragraph(p.page_index, p.paragraph_index).split())
                for p in focus.paragraphs
            ),
            "a paragraph's word count disagrees with the text Merge Memory holds",
        )
        stage.check(
            "the paragraph the reader pointed into was observed",
            (engine.state.pointer.page_index, engine.state.pointer.paragraph_index) in analysed,
            f"the reader finished in ¶{engine.state.pointer.paragraph_index}, "
            f"which the analysis never saw",
        )
        stage.check(
            "the analysis covers no more text than the page holds",
            0 < focus.total_words <= memory.total_words,
            f"{focus.total_words} words analysed, {memory.total_words} held",
        )
        stage.check(
            "the meaning request was charged to a paragraph",
            sum(p.meaning_requests for p in focus.paragraphs) == analytics.meaning_requests,
            f"focus counted {sum(p.meaning_requests for p in focus.paragraphs)}, "
            f"analytics {analytics.meaning_requests}",
        )
        stage.check(
            "the lookups recorded are the lookups analysed",
            sum(p.lookups for p in focus.paragraphs) == len(engine.lookups),
            f"focus counted {sum(p.lookups for p in focus.paragraphs)}, "
            f"the engine recorded {len(engine.lookups)}",
        )
        stage.check(
            "idle time is a part of the time in the paragraph, not an addition to it",
            all(p.idle_ms <= p.actual_ms for p in focus.paragraphs),
            "a paragraph is idle for longer than the reader was in it",
        )

        # Both engines run their own reading clock off the same call sites, so
        # they should agree to within scheduling jitter. They stop agreeing the
        # moment one keeps counting through something the other excludes — which
        # is the failure this exists to catch, and it would be worth seconds.
        #
        # Compared against the *tracker's* clock, not `analytics.reading_duration_ms`.
        # When TTS ran, `_totals` prefers the Audio Engine's `reading_time_ms`,
        # which counts time spent speaking rather than time spent reading; the two
        # are different quantities and only one of them is what focus measures.
        # The tracker survives `finish_session` precisely so it can still be read.
        final = speed.tracker(SESSION_ID).snapshot()
        observed_ms = sum(p.actual_ms for p in focus.paragraphs)
        report.field("reading clock (focus)", observed_ms)
        report.field("reading clock (speed)", final.elapsed_reading_ms)
        stage.check(
            "the focus analysis and Reading Speed agree on how long the reader read",
            abs(observed_ms - final.elapsed_reading_ms) <= 250,
            f"focus saw {observed_ms}ms of reading, Reading Speed "
            f"{final.elapsed_reading_ms}ms",
        )
        stage.check(
            "the seconds spent on the lookup are not reading time",
            observed_ms <= final.elapsed_wall_ms - meaning_mode_ms + 250,
            f"{observed_ms}ms of reading in a {final.elapsed_wall_ms}ms session "
            f"that spent {meaning_mode_ms}ms in Meaning Mode",
        )

        # The philosophy of the module, asserted against a real page rather than a
        # fixture. HIGH is the one verdict that needs both halves of the evidence,
        # and a slow paragraph with nothing else to say is UNKNOWN — because the
        # alternative is reporting every interruption as a comprehension problem.
        stage.check(
            "no paragraph was called difficult on timing alone",
            all(
                p.meaning_requests + p.revisits >= 2
                for p in focus.paragraphs
                if p.difficulty is DifficultyLevel.HIGH
            ),
            "a paragraph was rated HIGH with no meaning requests and no re-reads",
        )
        stage.check(
            "unjudged paragraphs are excluded from the ranking, not ranked last",
            all(p.difficulty is not DifficultyLevel.UNKNOWN for p in focus.ranked),
            "an UNKNOWN paragraph appears in the ranking",
        )
        stage.check(
            "the report never says the reader was distracted",
            not any(
                "distract" in line.lower()
                for p in focus.paragraphs
                for line in p.evidence
            ),
            "the specification's wording was not used",
        )
        stage.check(
            "idle time is reported as possible, not as fact",
            all(
                "possible idle time" in " ".join(p.evidence)
                for p in focus.paragraphs
                if p.had_idle_time
            ),
            "a paragraph carries idle time that the evidence does not hedge",
        )

        # Observation only, checked at the two places it could fail: reading the
        # report a second time must not move a baseline, and nothing the engine did
        # may have touched the pointer, the page or the text.
        second_read = engine.focus.report(speed.baseline_for(READER_ID))
        stage.check(
            "reading the report is idempotent",
            second_read.paragraphs == focus.paragraphs,
            "asking twice produced two different analyses",
        )
        stage.check(
            "the analysis did not move the reader's baseline",
            speed.baseline_for(READER_ID).baseline_wpm == baseline_before.baseline_wpm
            and speed.baseline_for(READER_ID).method is baseline_before.method,
            f"baseline moved from {baseline_before.baseline_wpm} to "
            f"{speed.baseline_for(READER_ID).baseline_wpm}",
        )
        stage.check(
            "the analysis is judged against the baseline the session ended with",
            focus.baseline_wpm == baseline_before.baseline_wpm,
            f"report says {focus.baseline_wpm}, the reader's baseline is "
            f"{baseline_before.baseline_wpm}",
        )
        stage.check(
            "the analysis left Merge Memory and the pointer where it found them",
            memory.version == engine.state.content_version
            and engine.state.pointer.page_index == memory.current_page,
            "an observer changed the thing it was observing",
        )

        judged = len(focus.ranked)
        stage.detail = (
            f"{focus.paragraphs_analysed} paragraph(s), {judged} judged, "
            f"{len(attention)} needing attention"
        )
        report.close(stage)

    ok = report.summary()

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as handle:
            json.dump(
                {
                    "image": str(image_path),
                    "passed": ok,
                    "vision_from_cache": cached,
                    "selected_word": selection.selected_word,
                    "selection_confidence": selection.confidence,
                    "sentence": sentence,
                    "paragraph": paragraph,
                    "page_text": memory.page_text(memory.current_page),
                    "merge_memory_version": memory.version,
                    "ai_prompt": recorder.calls[0][1] if recorder.calls else "",
                    "explanation": explanation.data if explanation else {},
                    "focus": focus.model_dump(mode="json") if focus else {},
                    "stages": [
                        {
                            "number": s.number,
                            "name": s.name,
                            "passed": s.passed,
                            "checks": [
                                {"label": label, "passed": passed, "detail": detail}
                                for label, passed, detail in s.checks
                            ],
                        }
                        for s in report.stages
                    ],
                },
                handle,
                ensure_ascii=False,
                indent=2,
            )
        print(f"\n  written to {out}")

    return 0 if ok else 1


# ----------------------------------------------------------------- helpers


def decode_bgr(data: bytes):
    """The photograph as a BGR array, for the finger detector.

    Deliberately the *original* bytes, not the preprocessed ones OCR submitted:
    `_enhance` returns a greyscale JPEG, and the contour fallback segments skin in
    HSV, which greyscale has none of. Preprocessing does not resize, so the boxes
    Vision reported still line up with this array pixel for pixel.
    """

    try:
        import cv2
        import numpy as np
    except ImportError:  # pragma: no cover
        return None
    return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)


def _contains_words(haystack: str, needle: str) -> bool:
    """Whether every word of `needle` appears in `haystack`.

    Word sets rather than substring: the sentence comes from the gesture
    selector's word walk and the paragraph from the same walk one level up, so
    they agree on words but not necessarily on the whitespace between them.
    """

    have = {w.strip(".,;:!?\"'“”‘’()[]").lower() for w in haystack.split()}
    want = {w.strip(".,;:!?\"'“”‘’()[]").lower() for w in needle.split()}
    want.discard("")
    return want <= have


def _narrated_paragraphs(memory: Any, page: int, spoken: list[str]) -> set[int]:
    """Which paragraphs of the held page the spoken lines came from.

    Derived here rather than recorded by the provider, which sees only text: a
    `SpeechRequest` carries no pointer. Matching each line back to the paragraphs
    that contain its words is enough to answer the question stage 8 asks — was
    everything narrated drawn from the page Merge Memory holds — without widening
    the provider contract for the benefit of a test harness.
    """

    found: set[int] = set()
    for index in range(memory.paragraph_count(page)):
        text = memory.paragraph(page, index)
        if any(_contains_words(text, line) for line in spoken):
            found.add(index)
    return found


def _prompt_paragraph(user_content: str) -> str:
    """Pull the paragraph back out of the rendered prompt, to compare with memory."""

    for line in user_content.splitlines():
        if line.startswith("Current paragraph: "):
            return line[len("Current paragraph: ") :]
    return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("image", help="a photograph of a page with a finger pointing at a word")
    parser.add_argument(
        "--fresh-ocr",
        action="store_true",
        help="call Google Vision again even though a cached response exists",
    )
    parser.add_argument(
        "--no-ai",
        action="store_true",
        help="skip the AI Engine's Groq call (key 1) but run the same code path",
    )
    parser.add_argument(
        "--no-groq",
        action="store_true",
        help="skip both Groq calls: no reconstruction, no explanation",
    )
    parser.add_argument("--book-title", default="", help="book title for the AI prompt")
    parser.add_argument("--audience", default="", help="intended audience for the AI prompt")
    parser.add_argument("--ai-timeout", type=float, default=30.0)
    parser.add_argument(
        "--sentence-seconds",
        type=float,
        default=0.6,
        help="how long the fake provider takes per sentence; keeps narration in "
        "flight so Meaning Mode has something to interrupt",
    )
    parser.add_argument("--json", default="", help="also write the full result to this file")
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    load_environment()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
