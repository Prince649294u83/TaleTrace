"""Every photograph in a folder, through OCR and Gesture, one row each.

    python -m scripts.validate_corpus images/
    python -m scripts.validate_corpus images/ --json corpus.json
    python -m scripts.validate_corpus images/ --cached-only   # never call Vision

What this adds over running `validate_pipeline` N times
------------------------------------------------------
`validate_pipeline` proves one photograph survives the whole chain, in depth. It
cannot answer the questions that only exist across a corpus:

  - Does OCR survive *every* photograph, or only the one that was debugged?
  - How often does the finger detector find a fingertip, and by which method?
  - Does page-change detection *discriminate*, or does it just always say yes?

That last one is the reason this harness exists rather than being a loop in a
shell script. A page turn is detected by comparing the frame against the page
held, so it takes two images to test at all, and it has two failure modes that
look nothing alike:

    the same page photographed twice  ->  must NOT turn (or the reader loses
                                          their place mid-paragraph)
    two genuinely different pages     ->  must turn (or page 2 merges into
                                          page 1 and the pointer never advances)

A detector stuck at "always a new page" passes the second and fails the first; one
stuck at "never" does the reverse. Only a matrix over several images distinguishes
them, so this runs every ordered pair.

Per-image tolerance, corpus-level thresholds
--------------------------------------------
A single blurred photograph is not a defect — it is a photograph of a moving book.
So each image is scored independently and the run fails on *rates*: OCR must read
every image, and the fingertip detection rate must clear `--min-finger-rate`.
Which images failed is always printed, because "6 of 11 found a fingertip" is a
number to act on and "FAIL" is not.

Cost
----
Google Vision is called at most once per image, ever, through the same
`scripts.vision_cache` the other harnesses share — so the second run of a corpus
is free, and re-running after a parser change costs nothing. `--cached-only`
refuses to call Vision at all, for working offline or on a metered connection.

No Groq, no audio, no AI. This is the front half of the pipeline — the half that
touches a camera — measured over many frames. The reading loop is
`validate_pipeline`'s subject and is not repeated here.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.core.environment import load_environment  # noqa: E402
from backend.app.modules.gesture_engine.detector import detect_finger  # noqa: E402
from backend.app.modules.gesture_engine.pipeline import GesturePipeline  # noqa: E402
from backend.app.modules.gesture_engine.selection_models import (  # noqa: E402
    SelectionConfig,
    SelectionStatus,
)
from backend.app.modules.merge_memory.engine import MergeMemory  # noqa: E402
from backend.app.modules.ocr.pipeline import OcrPipeline  # noqa: E402
from backend.app.modules.ocr.providers import GoogleVisionProvider  # noqa: E402
from backend.app.modules.ocr.replay import ReplayAdapter  # noqa: E402
from backend.app.modules.ocr.vision_cache import read_cached  # noqa: E402
from scripts import vision_cache  # noqa: E402

IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".bmp", ".webp")


# ------------------------------------------------------------------ one image


@dataclass
class ImageOutcome:
    """What one photograph turned out to be. Every field is observed, none inferred."""

    path: Path
    cached: bool = False

    words: int = 0
    lines: int = 0
    paragraphs: int = 0
    boxed: int = 0
    monotonic: bool = False
    text_chars: int = 0

    finger_found: bool = False
    finger_method: str = ""
    finger_confidence: float = 0.0

    selection_status: str = ""
    selected_word: str = ""
    selection_confidence: float = 0.0
    selection_ms: float = 0.0
    word_is_real: bool = False

    merge_words: int = 0
    merge_paragraphs: int = 0

    error: str = ""

    # The Vision response this image read as, kept for the pair matrix below.
    # Carried on the outcome rather than re-derived in `run` because re-deriving
    # it means re-reading the file and running CLAHE over it a second time, which
    # is free at eleven images and is not at a hundred.
    response: Any = field(default=None, repr=False)

    @property
    def ocr_ok(self) -> bool:
        """OCR read this page: words, all boxed, in reading order.

        Geometry is part of the bar rather than a separate score, because a word
        without a box cannot be pointed at — the gesture selector has nothing to
        measure a fingertip against, so an unboxed word is invisible to the half
        of the product this harness exists to check.
        """

        return (
            not self.error
            and self.words > 0
            and self.boxed == self.words
            and self.monotonic
        )

    @property
    def selection_ok(self) -> bool:
        """A word was resolved *and* it is a word Vision actually read.

        Kept together deliberately: a confident selection of a word that is not on
        the page is worse than no selection, and scoring them apart would let the
        second failure hide inside a passing rate for the first.
        """

        return self.selection_status == SelectionStatus.SUCCESS.value and self.word_is_real

    def row(self) -> str:
        finger = (
            f"{self.finger_method[:10]:<10} {self.finger_confidence:.2f}"
            if self.finger_found
            else f"{'-':<10} ----"
        )
        selected = self.selected_word[:18] if self.selected_word else "-"
        # "LIVE" only when Vision was actually called. An image that failed before
        # reaching Vision is neither live nor cached, and labelling it "LIVE" would
        # read as a call that was paid for.
        source = "-    " if self.error and not self.cached else ("cache" if self.cached else "LIVE ")
        return (
            f"  {'ok ' if self.ocr_ok else 'FAIL':<4} "
            f"{self.path.name[:34]:<34} "
            f"{self.words:>5} "
            f"{self.lines:>4} "
            f"{self.paragraphs:>4} "
            f"{finger}  "
            f"{selected:<18} "
            f"{self.selection_confidence:.2f} "
            f"{source}"
        )


def decode_bgr(data: bytes):
    """The photograph as a BGR array, for the finger detector.

    The *original* bytes, not the preprocessed ones OCR submits: `_enhance`
    returns greyscale, and the contour fallback segments skin in HSV, which
    greyscale has none of. Preprocessing does not resize, so Vision's boxes still
    line up with this array pixel for pixel. Same reasoning as
    `validate_pipeline.decode_bgr` — and it is duplicated rather than imported
    because importing one harness into another to share six lines couples their
    argument parsing and their `__main__` blocks.
    """

    try:
        import cv2
        import numpy as np
    except ImportError:  # pragma: no cover
        return None
    return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)


def examine(path: Path, *, fresh: bool, cached_only: bool, config: SelectionConfig) -> ImageOutcome:
    """One photograph through OCR and Gesture. Never raises — records instead.

    A harness that stopped on the first unreadable image would report nothing
    about the twenty after it, which is the opposite of what a corpus run is for.
    """

    outcome = ImageOutcome(path=path)

    try:
        raw = path.read_bytes()
    except OSError as exc:
        outcome.error = f"unreadable file: {exc}"
        return outcome

    # Each image gets its own pipeline and memory. Sharing them would make every
    # image after the first a *merge* into the page already held, and this stage
    # is asking what each photograph reads as on its own — the merging question is
    # the pair matrix below, where it can be answered deliberately.
    pipeline = OcrPipeline(provider=ReplayAdapter())

    try:
        if cached_only:
            response = read_cached(vision_cache.preprocess(raw))
            if response is None:
                outcome.error = "no cached Vision response and --cached-only was given"
                return outcome
            outcome.cached = True
        else:
            response, _prepared, outcome.cached = vision_cache.fetch_once(
                path, fresh=fresh, note=lambda message: None
            )
    except SystemExit as exc:
        # `fetch_once` raises SystemExit for a missing key or an HTTP failure.
        # Caught rather than allowed to propagate: one image without a cached
        # response must not abort the other ninety-nine.
        outcome.error = str(exc).splitlines()[0]
        return outcome
    except Exception as exc:  # pragma: no cover - network shapes vary
        outcome.error = f"{type(exc).__name__}: {exc}"
        return outcome

    words = tuple(GoogleVisionProvider.parse_response(response))
    outcome.response = response
    outcome.words = len(words)
    outcome.lines = len({w.line_index for w in words})
    outcome.paragraphs = len({w.paragraph_index for w in words})
    outcome.boxed = sum(1 for w in words if w.bbox != (0, 0, 0, 0))
    indices = [w.word_index for w in words]
    outcome.monotonic = indices == sorted(indices)

    if not words:
        outcome.error = "Vision returned no words"
        return outcome

    # Through the real pipeline and a real Merge Memory, not just the parser, so
    # that a page which parses but cannot be held is still a failure here.
    memory = MergeMemory()
    frame = pipeline.update_memory(response)
    # `whole_page=True` because `update_memory` returns the merge of every frame of
    # the page, not a fragment of it — the same call the Reading Engine makes. No
    # `reconstruct` is passed to the memory, so this is the raw-OCR path: Groq is
    # the reading loop's subject, not this harness's.
    memory.apply_frame(frame.text, whole_page=True)
    outcome.text_chars = len(frame.text)
    outcome.merge_words = memory.total_words
    outcome.merge_paragraphs = memory.paragraph_count(memory.current_page)

    image = decode_bgr(raw)
    if image is None:
        outcome.error = "OpenCV could not decode the image"
        return outcome

    finger = detect_finger(image, config)
    if finger is not None:
        outcome.finger_found = True
        outcome.finger_method = finger.detection_method
        outcome.finger_confidence = finger.confidence

    selection = GesturePipeline(config=config).observe(image, words, finger=finger)
    outcome.selection_status = selection.status.value
    outcome.selected_word = selection.selected_word
    outcome.selection_confidence = selection.confidence
    outcome.selection_ms = selection.selection_time_ms
    outcome.word_is_real = bool(selection.selected_word) and any(
        w.text == selection.selected_word for w in words
    )
    return outcome


# ------------------------------------------------------- page-turn discrimination


@dataclass
class PairVerdict:
    """One ordered pair of images, and whether the second read as a new page."""

    held: str
    incoming: str
    same_image: bool
    turned: bool
    overlap_words: int


def page_turn_matrix(responses: dict[Path, Any]) -> list[PairVerdict]:
    """Every ordered pair, plus each image against itself.

    The self-pairs are the control and the more important half. Photographing the
    same page twice is what an ESP32 does thirty times a minute, and a detector
    that turned the page on those would reset the reading pointer mid-paragraph
    every time the reader's hand moved — a failure no single-image run can see.

    Geometric detection only. `confirm_page_change` is deliberately not wired: the
    Groq confirmation is a *second* opinion whose job is to veto a false turn, and
    measuring the geometry through it would report the pair as agreeing when what
    is being measured is whether the free check needs the paid one.
    """

    verdicts: list[PairVerdict] = []
    for held_path, held_response in responses.items():
        for incoming_path, incoming_response in responses.items():
            pipeline = OcrPipeline(provider=ReplayAdapter())
            pipeline.update_memory(held_response)
            held_words = {w.text.lower() for w in pipeline.words}

            incoming = pipeline.process_frame(incoming_response)
            turned = pipeline.detect_new_page(incoming)

            verdicts.append(
                PairVerdict(
                    held=held_path.name,
                    incoming=incoming_path.name,
                    same_image=held_path == incoming_path,
                    turned=turned,
                    overlap_words=len(held_words & {w.text.lower() for w in incoming}),
                )
            )
    return verdicts


# ---------------------------------------------------------------------- report


@dataclass
class Check:
    label: str
    passed: bool
    detail: str = ""


@dataclass
class CorpusReport:
    outcomes: list[ImageOutcome] = field(default_factory=list)
    pairs: list[PairVerdict] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def check(self, label: str, passed: bool, detail: str = "") -> None:
        self.checks.append(Check(label, bool(passed), detail))

    def note(self, message: str) -> None:
        self.notes.append(message)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks)


def print_images(report: CorpusReport) -> None:
    print(f"\n{'=' * 110}")
    print("  PER IMAGE — OCR and gesture")
    print("=" * 110)
    print(
        f"  {'':<4} {'image':<34} {'words':>5} {'ln':>4} {'¶':>4} "
        f"{'finger':<10} {'conf':<5} {'selected':<18} {'sel':<4} src"
    )
    for outcome in report.outcomes:
        print(outcome.row())
        if outcome.error:
            print(f"       error: {outcome.error}")


def print_pairs(report: CorpusReport, *, verbose: bool) -> None:
    if not report.pairs:
        return

    self_pairs = [p for p in report.pairs if p.same_image]
    cross_pairs = [p for p in report.pairs if not p.same_image]

    print(f"\n{'=' * 110}")
    print("  PAGE-TURN DISCRIMINATION — the same page twice must not turn")
    print("=" * 110)
    print(f"  same image, re-photographed   {len(self_pairs)} pair(s)")
    print(f"    turned (false page turns)   {sum(1 for p in self_pairs if p.turned)}")
    print(f"  different images              {len(cross_pairs)} pair(s)")
    print(f"    turned (page change seen)   {sum(1 for p in cross_pairs if p.turned)}")

    false_turns = [p for p in self_pairs if p.turned]
    for pair in false_turns:
        print(f"    FALSE TURN  {pair.held} -> itself  (overlap {pair.overlap_words} words)")

    if verbose:
        print("\n  every cross pair:")
        for pair in cross_pairs:
            mark = "turn" if pair.turned else "hold"
            print(
                f"    {mark}  {pair.held[:30]:<30} -> {pair.incoming[:30]:<30} "
                f"overlap {pair.overlap_words:>4}"
            )


def print_summary(report: CorpusReport) -> bool:
    print(f"\n{'=' * 110}")
    print("  ACCEPTANCE")
    print("=" * 110)
    for check in report.checks:
        mark = "PASS" if check.passed else "FAIL"
        print(f"  [{mark}] {check.label}" + (f" — {check.detail}" if check.detail else ""))

    for note in report.notes:
        print(f"  note: {note}")

    failed = [c for c in report.checks if not c.passed]
    print()
    print(f"  {len(report.checks) - len(failed)} of {len(report.checks)} checks passed")
    return not failed


# ------------------------------------------------------------------------- run


def collect_images(target: Path, *, limit: int) -> list[Path]:
    """Every image under `target`, sorted. A single file is a corpus of one."""

    if target.is_file():
        return [target]

    found = sorted(
        path
        for path in target.rglob("*")
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
    )
    return found[:limit] if limit else found


def run(args: argparse.Namespace) -> int:
    target = Path(args.images).expanduser()
    if not target.exists():
        raise SystemExit(f"No such file or directory: {target}")

    images = collect_images(target, limit=args.limit)
    if not images:
        raise SystemExit(f"No images found under {target}")

    report = CorpusReport()
    config = SelectionConfig()

    print("TaleTrace — multi-image OCR and gesture validation")
    print(f"  target                  {target}")
    print(f"  images                  {len(images)}")
    print(f"  Vision                  {'cache only' if args.cached_only else 'live once, then cached'}")
    print(f"  finger rate required    {args.min_finger_rate:.0%}")

    responses: dict[Path, Any] = {}
    for index, path in enumerate(images, start=1):
        print(f"  [{index}/{len(images)}] {path.name}", flush=True)
        outcome = examine(
            path, fresh=args.fresh_ocr, cached_only=args.cached_only, config=config
        )
        report.outcomes.append(outcome)
        if outcome.ocr_ok and outcome.response is not None:
            # Only images OCR could read: a pair involving an unreadable frame
            # would measure the failure, not the detector.
            responses[path] = outcome.response

    print_images(report)

    readable = [o for o in report.outcomes if o.ocr_ok]
    with_finger = [o for o in report.outcomes if o.finger_found]
    with_selection = [o for o in report.outcomes if o.selection_ok]

    print(f"\n{'=' * 110}")
    print("  RATES")
    print("=" * 110)
    print(f"  images                       {len(report.outcomes)}")
    print(f"  OCR read the page            {len(readable)} ({_rate(len(readable), report):.0%})")
    print(f"  fingertip found              {len(with_finger)} ({_rate(len(with_finger), report):.0%})")
    print(f"  word resolved and real       {len(with_selection)} ({_rate(len(with_selection), report):.0%})")
    if readable:
        print(f"  median words per page        {_median([o.words for o in readable]):.0f}")
        print(f"  median paragraphs per page   {_median([o.paragraphs for o in readable]):.0f}")
    if with_selection:
        print(f"  median selection time        {_median([o.selection_ms for o in with_selection]):.1f} ms")
        print(f"  median selection confidence  {_median([o.selection_confidence for o in with_selection]):.2f}")

    methods: dict[str, int] = {}
    for outcome in with_finger:
        methods[outcome.finger_method] = methods.get(outcome.finger_method, 0) + 1
    for method, count in sorted(methods.items(), key=lambda kv: -kv[1]):
        print(f"  detected by {method:<16} {count}")

    if len(responses) >= 2:
        report.pairs = page_turn_matrix(responses)
        print_pairs(report, verbose=args.verbose)
    else:
        report.note(
            f"page-turn discrimination needs two readable images, {len(responses)} available — "
            "the self-pair control and the cross-pair matrix were both skipped"
        )

    # ------------------------------------------------------------- acceptance
    #
    # Rates, not per-image verdicts. One blurred photograph is a photograph of a
    # moving book, not a defect; a detector that finds a fingertip in a third of
    # them is a defect even though every individual failure looks excusable.

    report.check(
        "OCR read every image",
        len(readable) == len(report.outcomes),
        f"{len(report.outcomes) - len(readable)} of {len(report.outcomes)} unreadable: "
        + ", ".join(o.path.name for o in report.outcomes if not o.ocr_ok),
    )
    report.check(
        "every word Vision read carries a bounding box",
        all(o.boxed == o.words for o in readable),
        "a word without geometry cannot be pointed at",
    )
    report.check(
        "reading order is monotonic on every page",
        all(o.monotonic for o in readable),
        ", ".join(o.path.name for o in readable if not o.monotonic),
    )
    report.check(
        "Merge Memory holds every page OCR read",
        all(o.merge_words > 0 for o in readable),
        ", ".join(o.path.name for o in readable if o.merge_words == 0),
    )
    report.check(
        f"fingertip detection rate is at least {args.min_finger_rate:.0%}",
        _rate(len(with_finger), report) >= args.min_finger_rate,
        f"{len(with_finger)} of {len(report.outcomes)} images",
    )
    report.check(
        "every resolved word is one Vision actually read",
        all(
            o.word_is_real
            for o in report.outcomes
            if o.selection_status == SelectionStatus.SUCCESS.value
        ),
        "the selector returned a word that is not on the page",
    )
    report.check(
        "no selection succeeded below the confidence threshold",
        all(
            o.selection_confidence >= config.confidence_threshold
            for o in report.outcomes
            if o.selection_status == SelectionStatus.SUCCESS.value
        ),
        f"a SUCCESS was returned under {config.confidence_threshold}",
    )

    if report.pairs:
        self_pairs = [p for p in report.pairs if p.same_image]
        cross_pairs = [p for p in report.pairs if not p.same_image]
        false_turns = [p for p in self_pairs if p.turned]

        report.check(
            "the same page photographed twice is never a page turn",
            not false_turns,
            f"{len(false_turns)} of {len(self_pairs)} images turned the page on themselves",
        )
        # A detector that says "new page" to everything passes every cross-pair
        # check by accident, so the discriminating question is whether the two
        # populations differ at all — not whether either is high.
        report.check(
            "page detection discriminates rather than always answering the same way",
            not cross_pairs
            or len({p.turned for p in self_pairs} | {p.turned for p in cross_pairs}) > 1
            or all(not p.turned for p in self_pairs),
            "every pair got the same verdict — the detector is stuck",
        )

    ok = print_summary(report)

    if len(images) < args.expect_images:
        # Printed after the verdict and deliberately not a check. A corpus smaller
        # than the target is a gap in the *evidence*, not a fault in the code, and
        # failing the run for it would make a green result mean "someone found
        # enough photographs" instead of "the pipeline held".
        print(
            f"\n  COVERAGE GAP: ran {len(images)} image(s), target is {args.expect_images}. "
            "These checks held on what was available; they are not yet evidence at scale."
        )

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as handle:
            json.dump(
                {
                    "target": str(target),
                    "images": len(images),
                    "passed": ok,
                    "rates": {
                        "ocr_ok": len(readable),
                        "finger_found": len(with_finger),
                        "selection_ok": len(with_selection),
                        "total": len(report.outcomes),
                    },
                    "outcomes": [
                        {
                            "image": o.path.name,
                            "cached": o.cached,
                            "words": o.words,
                            "lines": o.lines,
                            "paragraphs": o.paragraphs,
                            "boxed": o.boxed,
                            "monotonic": o.monotonic,
                            "merge_words": o.merge_words,
                            "merge_paragraphs": o.merge_paragraphs,
                            "finger_found": o.finger_found,
                            "finger_method": o.finger_method,
                            "finger_confidence": o.finger_confidence,
                            "selection_status": o.selection_status,
                            "selected_word": o.selected_word,
                            "selection_confidence": o.selection_confidence,
                            "selection_ms": o.selection_ms,
                            "word_is_real": o.word_is_real,
                            "ocr_ok": o.ocr_ok,
                            "error": o.error,
                        }
                        for o in report.outcomes
                    ],
                    "pairs": [
                        {
                            "held": p.held,
                            "incoming": p.incoming,
                            "same_image": p.same_image,
                            "turned": p.turned,
                            "overlap_words": p.overlap_words,
                        }
                        for p in report.pairs
                    ],
                    "checks": [
                        {"label": c.label, "passed": c.passed, "detail": c.detail}
                        for c in report.checks
                    ],
                    "notes": report.notes,
                },
                handle,
                ensure_ascii=False,
                indent=2,
            )
        print(f"\n  written to {out}")

    return 0 if ok else 1


def _rate(count: int, report: CorpusReport) -> float:
    return count / len(report.outcomes) if report.outcomes else 0.0


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("images", help="a directory of photographs, or one image file")
    parser.add_argument(
        "--limit", type=int, default=0, help="stop after this many images (0 = all)"
    )
    parser.add_argument(
        "--expect-images",
        type=int,
        default=50,
        help="corpus size this harness is meant to run at; a shortfall is reported, not failed",
    )
    parser.add_argument(
        "--min-finger-rate",
        type=float,
        default=0.5,
        help="fraction of images that must yield a fingertip",
    )
    parser.add_argument(
        "--cached-only",
        action="store_true",
        help="never call Google Vision; an image with no cached response is an error",
    )
    parser.add_argument(
        "--fresh-ocr", action="store_true", help="call Vision again for every image"
    )
    parser.add_argument(
        "--verbose", action="store_true", help="print every cross pair of the page-turn matrix"
    )
    parser.add_argument("--json", default="", help="also write the full result to this file")
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    load_environment()
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
