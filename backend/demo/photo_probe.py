"""Run the real OCR and Gesture chain against one photograph.

    python -m backend.demo.photo_probe "path/to/page.jpeg"

Everything else in `demo/` replays recorded word boxes, which proves the wiring
but not the recognition. This takes an actual photo of an actual page and pushes
it through the production path:

    JPEG ──► GoogleVisionProvider ──► OcrPipeline ──► MergeMemory
                                          │
                                          └─ words ──► GesturePipeline ──► events

No fakes. The only thing supplied by hand is the fingertip, and only when
MediaPipe finds no hand in the frame — a photo of a page usually has no hand in
it, and the selector still needs to be shown working against these real boxes.

Requires `GOOGLE_VISION_API_KEY`. `--env` points at the file holding it so the
key never has to be pasted onto a command line, where it would land in shell
history.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from backend.app.modules.gesture_engine.pipeline import GesturePipeline
from backend.app.modules.gesture_engine.selection_models import FingerPoint
from backend.app.modules.merge_memory.engine import MergeMemory
from backend.app.modules.ocr.pipeline import OcrPipeline
from backend.app.modules.ocr.providers import OcrProviderError, get_ocr_engine


def load_env(path: Path) -> list[str]:
    """Read `KEY=value` lines into the environment. Returns the names, not the values.

    Names only, deliberately: this prints what it loaded so a failed run can be
    diagnosed, and a key echoed to a terminal is a key in the scrollback.
    """

    loaded: list[str] = []
    if not path.is_file():
        return loaded
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value:
            os.environ.setdefault(key, value)
            loaded.append(key)
    return loaded


def run_ocr(image: Path) -> tuple[OcrPipeline, MergeMemory, object]:
    """The OCR half: photo to versioned page text in Merge Memory."""

    provider = get_ocr_engine()
    pipeline = OcrPipeline(provider)
    memory = MergeMemory()

    print(f"  provider           {provider.provider_name}")
    print(f"  image              {image.name}  ({image.stat().st_size // 1024} KB)")
    print()

    # `update_memory` rather than `process_frame`: the former is the production
    # entry point and the one that versions, merges and reports a page turn.
    result = pipeline.update_memory(str(image))

    print("── OCR pipeline ──────────────────────────────────────────────")
    print(f"  accepted           {result.accepted}")
    print(f"  reason             {result.reason or '(none)'}")
    print(f"  words recognised   {result.word_count}")
    print(f"  page changed       {result.page_changed}")
    print(f"  pipeline version   {result.version}")
    if result.words:
        mean = sum(w.confidence for w in result.words) / len(result.words)
        worst = min(result.words, key=lambda w: w.confidence)
        print(f"  mean confidence    {mean:.3f}")
        print(f"  least confident    '{worst.text}' at {worst.confidence:.3f}")
        lines = len({w.line_index for w in result.words})
        paragraphs = len({w.paragraph_index for w in result.words})
        print(f"  layout             {paragraphs} paragraph(s), {lines} line(s)")
    print()

    if result.text:
        # `whole_page=True`: this is the pipeline's merged page, not a fragment of
        # one. Appending it would add the page to itself.
        version = memory.apply_frame(result.text, whole_page=True)
        print("── Merge Memory ──────────────────────────────────────────────")
        print(f"  version            {version}")
        print(f"  pages held         {memory.page_count}")
        print(f"  words held         {memory.total_words}")
        print(f"  paragraphs         {memory.paragraph_count(memory.current_page)}")
        content = memory.content_map(source_version=version)
        print(f"  sentences          {len(content.sentences)}")
        print()

    return pipeline, memory, result


def run_gesture(image: Path, words, *, target: str = "") -> None:
    """The Gesture half: the same frame and the same words, through the selector.

    Two passes. The first asks MediaPipe to find a hand, which is the production
    path. A photo of a page usually has no hand in it, so the second supplies a
    fingertip on a real word box and shows the selector resolving it — the part
    that would otherwise go unexercised on a hand-free photo.
    """

    print("── Gesture pipeline ──────────────────────────────────────────")
    if not words:
        print("  skipped — no words to point at")
        print()
        return

    events: list[tuple[str, dict]] = []
    gestures = GesturePipeline(publish=lambda event, payload: events.append((event.value, payload)))

    # Pass one: real detection on the real frame.
    try:
        import cv2

        frame = cv2.imread(str(image))
    except ImportError:
        frame = None
        print("  detector           skipped (OpenCV not installed)")

    if frame is not None:
        detected = gestures.process_frame(frame, list(words))
        print(f"  MediaPipe          {detected.status.value}")
        if detected.selected_word:
            print(f"  selected           '{detected.selected_word}'")

    # Pass two: a fingertip placed on a known word box.
    chosen = _pick_target(words, target)
    finger = FingerPoint(
        x=float(chosen.center_x),
        y=float(chosen.center_y),
        confidence=0.95,
    )
    print(f"  fingertip          ({finger.x:.0f}, {finger.y:.0f}) — centre of '{chosen.text}'")

    result = gestures.observe(frame, list(words), finger=finger)
    print(f"  status             {result.status.value}")
    print(f"  selected           '{result.selected_word or '(none)'}'")
    if getattr(result, "confidence", None) is not None:
        print(f"  confidence         {result.confidence:.3f}")
    if getattr(result, "sentence", ""):
        print(f"  sentence           {result.sentence[:70]}")

    print(f"  events published   {len(events)}")
    for name, payload in events:
        detail = payload.get("word") or payload.get("reason") or ""
        print(f"    - {name} {detail}")
    print()

    if result.selected_word and chosen.text.strip(".,;:!?\"'") not in result.selected_word:
        print(f"  NOTE: asked for '{chosen.text}', selector resolved '{result.selected_word}'")
        print()


def _pick_target(words, requested: str):
    """The word to point at: the one asked for, else the longest real word.

    Longest as the default because a short filler word is a correct but
    unconvincing selection — it looks like the selector missed.
    """

    if requested:
        for word in words:
            if word.text.strip(".,;:!?\"'").lower() == requested.lower():
                return word
        print(f"  (no word '{requested}' on this page; using the longest instead)")

    real = [w for w in words if len(w.text.strip(".,;:!?\"'")) > 3]
    return max(real or list(words), key=lambda w: len(w.text))


def main(argv: list[str] | None = None) -> int:
    # UTF-8 on stdout before anything is printed. The page text comes from a real
    # book and will contain curly quotes and dashes whatever this file does; the
    # console is the thing that has to cope. Windows still defaults to cp1252.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):  # pragma: no cover - not a tty
        pass

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path, help="photograph of a book page")
    parser.add_argument(
        "--env",
        type=Path,
        default=Path("backend/app/OCRandGESTURE/.env"),
        help="file holding GOOGLE_VISION_API_KEY",
    )
    parser.add_argument("--word", default="", help="word to point at (default: the longest)")
    args = parser.parse_args(argv)

    if not args.image.is_file():
        print(f"No such image: {args.image}", file=sys.stderr)
        return 2

    print()
    print("═══ TaleTrace — real OCR + Gesture on one photograph ═════════")
    loaded = load_env(args.env)
    print(f"  env                {args.env} ({', '.join(loaded) or 'nothing loaded'})")

    try:
        pipeline, memory, result = run_ocr(args.image)
    except OcrProviderError as error:
        # Named separately from an unexpected crash: this is the provider saying
        # no, and the fix is a credential or a network, not a code change.
        print(f"\n  OCR provider failed: {error}", file=sys.stderr)
        return 1

    run_gesture(args.image, result.words, target=args.word)

    print("── Recognised page text ──────────────────────────────────────")
    print(memory.page_text(memory.current_page) or "(nothing)")
    print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
