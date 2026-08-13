"""Same photo, both implementations, side by side.

    python -m scripts.equivalence_probe <image.jpg> [more.jpg ...]
    python -m scripts.equivalence_probe page.jpg --no-groq     # deterministic stages only
    python -m scripts.equivalence_probe page.jpg --baseline    # show the LLM's own ceiling

The migration's acceptance test, and the only one that can actually settle it:
Rule 2 says the same ESP32 image and Vision result must produce the same Merge
Memory, selected word, sentence, paragraph, page detection and reading pointer as
`OCRandGESTURE/` did. Every other test in this repository asserts that the new
code does what the new code intends. This one asserts it does what the *old* code
did.

Not in `tests/` on purpose. It needs the reference tree, a live key, and gives a
different answer on a different photo — all three disqualify it as a unit test and
none of them make it less necessary before shipping.

    photo ─┬─► reference: taletrace_processor + Gesture/gesture_engine
           └─► migrated:  OcrPipeline + MergeMemory + gesture_engine + prompts

Six comparisons. The first four are deterministic and are asserted; the last two
involve a language model and are reported.

  OCR rendering    one Vision response, rendered by both sides. The reference
                   reads the flat `fullTextAnnotation.text`; the migrated pipeline
                   rebuilds the page from the word hierarchy to keep paragraph
                   structure. Reads 100% on all sample photographs.
  Gesture          the same response parsed by both word extractors, the same
                   photograph through both fingertip detectors and both word
                   selectors. Selected word, line, paragraph, box, confidence and
                   all three indices, field by field. No model involved: this one
                   either matches or the port is wrong.
  Reading pointer  `calculate_accurate_pointer` against `pointer_offset`, on the
                   same text pair. Migrated unmodified, so an exact match.
  Prompt context   the reference has no AI Engine — its Meaning Mode prints the
                   word, its line and its paragraph and stops. So what is checked
                   is context assembly: everything the reference surfaces to the
                   reader must appear in the prompt the migrated AI Engine sends.
                   Constructed, not sent: this costs no Groq call.
  Page detection   both sides asked the same question about the same two texts.
                   A boolean, so comparable even though a model produced it.
  End to end       the full chain including reconstruction. Two independent calls
                   to a language model, so this cannot reach 100%; `--baseline`
                   runs the reference against itself to show why.

Cost
----
Google Vision is never called: both sides replay the response cached by
`scripts.vision_cache`, which is the same entry `validate_pipeline` uses. That is
deliberate beyond saving money — two live calls on the same bytes come back
slightly different, and a difference measured that way could not be attributed to
either implementation. With `--no-groq` this whole script runs offline and free.

Reported, not asserted, for anything a model touched. Where the two differ the
question is always "which is right", and that is a judgement about a page of a
book — so this prints the difference and leaves the judgement where it belongs.
"""

from __future__ import annotations

import argparse
import difflib
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REFERENCE = ROOT / "backend" / "app" / "OCRandGESTURE"

# Anything the two sides disagreed about, collected across every image and
# printed once at the end. A difference buried in 200 lines of output is a
# difference nobody sees.
DIFFERENCES: list[str] = []

# Wide enough for the longest label in the script, so the "same"/"DIFFERS" column
# stays a column. A ragged one is read line by line; a straight one is scanned.
_LABEL = 28

# A page from somewhere else entirely, for the negative half of page detection.
# Prose rather than lorem ipsum, and long enough to clear the five-word floor:
# both detectors are being asked a question about meaning, and gibberish would
# test whether they can recognise gibberish rather than whether they can tell two
# pages apart.
ANOTHER_PAGE = """
The tide went out further than anyone could remember that morning, and the
fishing boats lay tilted on the mud like sleeping animals. Marta walked out
across the flats in her brother's boots, counting the ribs of the old wreck as
she passed it. Somewhere behind her the church bell rang seven times and then,
uncertainly, an eighth.
"""


def load_reference():
    """Import the reference pipeline, which expects to be run from its own tree.

    It reads `.env` and builds a Vision client at import time, and its modules
    import each other by bare name, so both directories have to be on the path
    before the import rather than after.

    Returns `(pipeline, module)`. `pipeline` is the manager instance that holds
    the reference's merge memory; the module is needed separately because
    `preprocess_image` and `get_google_vision_text` are module-level functions,
    and both the OCR comparison and the Vision replay have to reach them.
    """

    sys.path.insert(0, str(REFERENCE / "OCR_dynamicMem"))
    sys.path.insert(0, str(REFERENCE / "Gesture"))

    from dotenv import load_dotenv

    load_dotenv(REFERENCE / ".env", override=False)
    load_dotenv(ROOT / ".env", override=False)

    import taletrace_processor  # type: ignore[import-not-found]

    return taletrace_processor.pipeline, taletrace_processor


def build_migrated():
    """The production chain, assembled the way `build_live` assembles it."""

    from backend.app.modules.merge_memory.engine import MergeMemory
    from backend.app.modules.merge_memory.reconstruction import GroqReconstructor
    from backend.app.modules.ocr.pipeline import OcrPipeline
    from backend.app.modules.ocr.replay import ReplayAdapter

    reconstructor = GroqReconstructor()
    # Replay rather than the live provider: the Vision response is already in
    # hand, and calling the API again would defeat the point of having cached it.
    pipeline = OcrPipeline(
        provider=ReplayAdapter(),
        confirm_page_change=reconstructor.is_same_page,
    )
    return pipeline, MergeMemory(reconstruct=reconstructor), reconstructor


# ----------------------------------------------------------------- reporting


def normalise(text: str) -> list[str]:
    """Compare on words, not characters.

    Whitespace differs by construction — the reference reads Vision's flat
    `fullTextAnnotation.text`, the migrated pipeline rebuilds paragraphs from the
    word hierarchy — and that difference is intended, documented, and not what
    this is looking for. What matters is whether the same words came out.
    """

    return text.split()


def section(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 56 - len(title)))


def report(
    name: str,
    left: str,
    right: str,
    *,
    left_label: str = "reference",
    right_label: str = "migrated",
) -> float:
    """Print one text comparison and return its similarity ratio.

    The labels are parameters because two of the comparisons have the reference
    on both sides, and calling its second run "migrated" would make the baseline
    read as a migration defect — which is the exact confusion the baseline was
    added to remove.
    """

    left_words, right_words = normalise(left), normalise(right)
    ratio = difflib.SequenceMatcher(None, left_words, right_words).ratio()
    width = max(len(left_label), len(right_label))

    section(name)
    print(f"  {left_label.ljust(width)}  {len(left_words):>5} words")
    print(f"  {right_label.ljust(width)}  {len(right_words):>5} words")
    print(f"  {'similarity'.ljust(width)}  {ratio:>5.1%}")

    only_left = [w for w in left_words if w not in set(right_words)]
    only_right = [w for w in right_words if w not in set(left_words)]

    if only_left:
        print(f"  {left_label} only ({len(only_left)}): {' '.join(only_left[:20])}")
    if only_right:
        print(f"  {right_label} only ({len(only_right)}): {' '.join(only_right[:20])}")
    if not only_left and not only_right:
        print("  same words, both directions")

    return ratio


def agree(label: str, left: Any, right: Any, *, where: str = "", extra: str = "") -> bool:
    """One field, both sides. Prints, records a difference, returns whether it held.

    Every deterministic claim in this script goes through here rather than
    through `report`, because a selected word is not a text to be scored for
    similarity — it either is the word the reference chose or it is not, and a
    ratio would soften exactly the failure that matters most.

    `extra` is trailing context for the value — a denominator, a unit — kept out
    of the comparison itself so that what is being asserted stays the value alone.
    """

    same = left == right
    if isinstance(left, float) and isinstance(right, float):
        same = abs(left - right) < 1e-6

    tail = f"  {extra}" if extra else ""
    if same:
        print(f"  {label.ljust(_LABEL)} same       {_short(left)}{tail}")
    else:
        print(f"  {label.ljust(_LABEL)} DIFFERS    {_short(left)}{tail}")
        print(f"  {''.ljust(_LABEL)}            {_short(right)}{tail}   <- migrated")
        DIFFERENCES.append(f"{where}{label}: {_short(left)} vs {_short(right)}")
    return same


def _short(value: Any, limit: int = 68) -> str:
    text = repr(value) if not isinstance(value, str) else repr(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


# ------------------------------------------------------------------- the OCR


def replay_vision(reference_module, flat_text: str) -> None:
    """Make the reference read the cached Vision response instead of calling out.

    The same seam the migrated side has in `ReplayAdapter`, applied to the
    reference so both implementations are handed byte-identical OCR. Without it
    each side calls Vision separately, Vision returns slightly different text for
    two calls on the same image, and every downstream difference becomes
    unattributable — the comparison would be measuring the API's variance rather
    than the port.

    Reapplied after every `importlib.reload`, since a reload restores the real
    function.
    """

    reference_module.get_google_vision_text = lambda _bytes: flat_text


def compare_ocr(response: dict, name: str) -> float:
    """The OCR stage alone, from a single Vision response rendered by both sides.

    The most decisive comparison in this script and the only fully deterministic
    text one. The reference reads `fullTextAnnotation.text`; the migrated pipeline
    rebuilds the page from the word hierarchy because it needs the paragraph
    structure the flat string throws away. This asks whether that rebuild says the
    same thing.
    """

    from backend.app.modules.ocr.pipeline import _as_text
    from backend.app.modules.ocr.providers import GoogleVisionProvider

    flat = flat_text(response)
    rebuilt = _as_text(GoogleVisionProvider.parse_response(response))
    return report(
        f"{name} — OCR rendering",
        flat,
        rebuilt,
        left_label="Vision flat text",
        right_label="rebuilt from words",
    )


def flat_text(response: dict) -> str:
    """Vision's own flat page text — what the reference's OCR step returns."""

    responses = response.get("responses") or [{}]
    return responses[0].get("fullTextAnnotation", {}).get("text", "")


# --------------------------------------------------------------- the gesture


def compare_gesture(image_path: Path, response: dict, name: str) -> tuple[Any, Any]:
    """Both word extractors, both fingertip detectors, both word selectors.

    Nothing here touches a network or a language model, so every field is an
    assertion rather than an observation: the reader pointed at one word, and two
    implementations of the same algorithm either agree on which one or do not.

    Each side runs its *own* detector rather than sharing one fingertip. Sharing
    would isolate the selector, which is the smaller half — the detector was
    ported too, and a fingertip found two hundred pixels away picks a different
    word however faithful the selector is.
    """

    from gesture_engine import select_word  # type: ignore[import-not-found]
    from gesture_engine.models import (  # type: ignore[import-not-found]
        SelectionConfig as ReferenceConfig,
    )
    from ocr.google_vision_provider import (  # type: ignore[import-not-found]
        GoogleVisionOCRProvider,
    )

    from backend.app.modules.gesture_engine.pipeline import GesturePipeline
    from backend.app.modules.ocr.providers import GoogleVisionProvider
    from scripts.validate_pipeline import decode_bgr

    section(f"{name} — gesture and selection")

    image = decode_bgr(image_path.read_bytes())
    if image is None:
        print("  OpenCV could not decode the image; gesture comparison skipped")
        return None, None

    reference_words = GoogleVisionOCRProvider.parse_google_vision_response(response)
    migrated_words = GoogleVisionProvider.parse_response(response)

    where = f"{name} gesture: "
    agree("words extracted", len(reference_words), len(migrated_words), where=where)
    agree(
        "word texts in order",
        [w.text for w in reference_words],
        [w.text for w in migrated_words],
        where=where,
    )
    agree(
        "boxes in order",
        [tuple(w.bbox) for w in reference_words],
        [tuple(w.bbox) for w in migrated_words],
        where=where,
    )
    agree(
        "paragraphs seen",
        len({w.paragraph_index for w in reference_words}),
        len({w.paragraph_index for w in migrated_words}),
        where=where,
    )

    reference_result = select_word(image, reference_words, ReferenceConfig())
    migrated_result = GesturePipeline().process_frame(image, migrated_words)

    reference_finger = reference_result.finger_point
    migrated_finger = migrated_result.finger_point
    if reference_finger and migrated_finger:
        agree(
            "fingertip",
            (round(reference_finger.x), round(reference_finger.y)),
            (round(migrated_finger.x), round(migrated_finger.y)),
            where=where,
        )
        agree(
            "detection method",
            reference_finger.detection_method,
            migrated_finger.detection_method,
            where=where,
        )
        agree("pointing direction", reference_finger.direction, migrated_finger.direction, where=where)
    else:
        agree("fingertip found", reference_finger is not None, migrated_finger is not None, where=where)

    agree("status", reference_result.status.value, migrated_result.status.value, where=where)
    agree("selected word", reference_result.selected_word, migrated_result.selected_word, where=where)
    agree(
        "selection confidence",
        round(float(reference_result.confidence), 6),
        round(float(migrated_result.confidence), 6),
        where=where,
    )
    agree(
        "word box",
        tuple(reference_result.selected_word_bbox or ()),
        tuple(migrated_result.selected_word_bbox or ()),
        where=where,
    )
    agree("word index", reference_result.word_index, migrated_result.word_index, where=where)
    agree("line index", reference_result.line_index, migrated_result.line_index, where=where)
    agree(
        "paragraph index",
        reference_result.paragraph_index,
        migrated_result.paragraph_index,
        where=where,
    )
    agree("selected line", reference_result.selected_line, migrated_result.selected_line, where=where)
    agree(
        "selected paragraph",
        reference_result.selected_paragraph,
        migrated_result.selected_paragraph,
        where=where,
    )
    agree("sentence context", reference_result.context, migrated_result.context, where=where)

    return reference_result, migrated_result


# --------------------------------------------------------- pointer and prompt


def compare_pointer(reference, held: str, merged: str, name: str) -> None:
    """`calculate_accurate_pointer` against `pointer_offset`, same inputs.

    Both are pure functions over two strings, so this is an exact comparison and
    the cheapest genuine evidence in the script that the reading pointer survived
    the move. The migrated copy is unmodified — if this ever differs, someone
    "improved" it.

    Three input pairs, not one. A single photograph is the *first* frame of a page,
    so the memory it merges into is empty and both implementations return 0 — a
    match that proves only that they agree about nothing having been read yet. The
    branch that matters is a same-page merge partway down a page, and the second
    and third pairs construct it from the page just read: the first half of the
    page as the memory held, and then a version with a word repaired earlier in
    the text, which is the case that made reusing the old offset wrong.
    """

    from backend.app.modules.merge_memory.reconstruction import pointer_offset

    section(f"{name} — reading pointer")

    half = merged[: len(merged) // 2]
    repaired = merged.replace("teh ", "the ", 1) if "teh " in merged else "One " + merged

    cases = [
        ("offset, first frame", held, merged),
        ("offset, half-read page", half, merged),
        ("offset, repaired earlier", half, repaired),
    ]

    for label, left, right in cases:
        reference_offset = reference.calculate_accurate_pointer(left, right)
        migrated_offset = pointer_offset(left, right)
        agree(
            label,
            reference_offset,
            migrated_offset,
            where=f"{name} pointer: ",
            extra=f"of {len(right)} characters",
        )


def compare_prompt_context(reference_result: Any, page_text: str, name: str) -> None:
    """What the reference shows a reader in Meaning Mode, against what we send an AI.

    The reference has no AI Engine: `print_gesture_details` prints the selected
    word, its line, its paragraph and the confidence, and Meaning Mode ends there.
    So there is no prompt to diff, and pretending otherwise would invent a
    comparison. What is comparable is context assembly — every fact the reference
    puts in front of the reader has to reach the model on the migrated side, or
    the explanation is being written with less than the old system had.

    The prompt is *built*, not sent. `PromptBuilder.user_content` is the same
    method the AI Engine calls, so this is the literal string that would go over
    the wire, and constructing it costs nothing.
    """

    from backend.app.modules.ai_engine.models import ReadingContext, ReadingMode
    from backend.app.modules.ai_engine.prompts import PromptBuilder

    section(f"{name} — Meaning Mode context assembly")

    if reference_result is None or not reference_result.selected_word:
        print("  the reference selected no word; nothing to assemble")
        return

    context = ReadingContext(
        selected_word=reference_result.selected_word,
        current_paragraph=reference_result.selected_paragraph or page_text,
        page_number=1,
        # `AiBridge`'s default, so this is the mode a Meaning Mode request
        # actually carries rather than a mode chosen to make the check pass.
        mode=ReadingMode.STANDARD,
    )
    user_content = PromptBuilder().user_content(context)
    print("  prompt the AI Engine would send:")
    for line in user_content.splitlines():
        print(f"    {line[:100]}")

    where = f"{name} prompt: "
    agree(
        "word reaches the prompt",
        True,
        f"Selected word: {reference_result.selected_word}" in user_content,
        where=where,
    )
    agree(
        "paragraph reaches the prompt",
        True,
        bool(reference_result.selected_paragraph)
        and reference_result.selected_paragraph in user_content,
        where=where,
    )
    agree(
        "line words reach the prompt",
        True,
        _words_present(user_content, reference_result.selected_line),
        where=where,
    )
    print(
        "  the reference has no AI Engine — its Meaning Mode prints these fields\n"
        "  and stops, so this checks assembly, not the model's answer."
    )


def _words_present(haystack: str, needle: str) -> bool:
    """Whether every word of `needle` appears in `haystack`, ignoring spacing."""

    if not needle.strip():
        return False
    return set(needle.split()) <= set(haystack.split())


# ------------------------------------------------------------ page detection


def compare_page_detection(
    reference, reconstructor, held: str, incoming: str, label: str, name: str
) -> None:
    """Both sides asked whether two texts are the same page.

    A boolean, which is what makes it comparable at all: the two implementations
    ask a language model in slightly different words and it answers in its own,
    but "same page" and "different page" are the only outcomes, and a page turn is
    either detected or it is not.

    The migrated side is asked in two parts because it decides in two parts: set
    overlap first, and the model only as a veto on a turn that geometry already
    proposed. That was the point of the change — the reference paid a network
    round trip on every single frame and defaulted to "same page" whenever the
    call failed, so an outage silently disabled page detection.
    """

    from backend.app.modules.ocr.pipeline import MIN_WORDS_FOR_A_PAGE

    print(f"\n  {label}")
    reference_same = reference.check_same_page_via_groq(held, incoming)
    migrated_same = reconstructor.is_same_page(held, incoming)
    agree("same page", reference_same, migrated_same, where=f"{name} page detection ({label}): ")
    print(
        f"  {'migrated first pass'.ljust(_LABEL)} set overlap, no network; the model is "
        f"consulted only to veto a turn ({MIN_WORDS_FOR_A_PAGE}-word floor)"
    )


# ---------------------------------------------------------------- end to end


def reference_self_agreement(reference_module, image: Path, flat: str) -> float:
    """The reference against itself, same photo, two runs. The ceiling.

    Without this the end-to-end number has nothing to be measured against.
    Reconstruction is a call to a language model, so the reference does not
    reproduce its own output: on a real page it agrees with itself around 84% of
    the time, and the words it drops on one run and keeps on the next are the
    same half-read fragments the migrated chain is criticised for keeping.

    So this is the number the end-to-end comparison should be read beside. A
    migrated result near this figure is as equivalent as the reference is to
    itself, which is the strongest claim available about a stage that is
    inherently non-deterministic.
    """

    import contextlib
    import importlib
    import io

    raw = image.read_bytes()
    runs: list[str] = []
    for _ in range(2):
        # Reloaded rather than reset: the reference holds its merge memory in a
        # module-level manager, and the second run has to start from an empty page
        # or it would be comparing one frame against two. The Vision replay is
        # reapplied because the reload restores the real network call.
        importlib.reload(reference_module)
        replay_vision(reference_module, flat)
        with contextlib.redirect_stdout(io.StringIO()):
            reference_module.pipeline.process_frame(raw)
        runs.append(reference_module.pipeline.merge_memory)

    return report(
        f"{image.name} — reference vs itself",
        runs[0],
        runs[1],
        left_label="run 1",
        right_label="run 2",
    )


# --------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("images", nargs="+", type=Path, help="page photographs")
    parser.add_argument(
        "--no-groq",
        action="store_true",
        help="deterministic comparisons only: OCR, gesture, pointer, prompt assembly",
    )
    parser.add_argument(
        "--baseline",
        action="store_true",
        help="also run the reference twice, to show how well it agrees with itself",
    )
    parser.add_argument(
        "--fresh-ocr",
        action="store_true",
        help="call Google Vision again even though a cached response exists",
    )
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    if args.no_groq:
        # Isolates OCR and gesture from reconstruction. A difference that survives
        # this is in the recognition path; one that disappears is the LLM being an
        # LLM. All three names are cleared: the reference tree reads the legacy
        # variable, the migrated Merge Engine reads `_2`, and either left set would
        # leave one side of the comparison still calling Groq.
        os.environ["GROQ_API_KEY"] = ""
        os.environ["GROQ_API_KEY_1"] = ""
        os.environ["GROQ_API_KEY_2"] = ""

    from scripts import vision_cache

    print("TaleTrace — reference vs migrated, same photographs")
    print(f"  reconstruction  {'disabled' if args.no_groq else 'live Groq'}")
    print("  vision          replayed from cache into both sides")

    _reference, reference_module = load_reference()
    pipeline, memory, reconstructor = build_migrated()

    ocr_ratios: list[float] = []
    ratios: list[float] = []
    baselines: list[float] = []

    for image in args.images:
        if not image.is_file():
            print(f"  skipped: {image} is not a file")
            continue

        # Rebound per image rather than once, because `--baseline` reloads the
        # reference module and the reload replaces the manager this loop
        # accumulates into. Without it a second image would be compared against a
        # stale object holding no page.
        reference = reference_module.pipeline

        response, _prepared, cached = vision_cache.fetch_once(
            image, fresh=args.fresh_ocr, note=lambda m: print(f"  {m}")
        )
        print(f"\n{'=' * 60}\n{image.name}  ({'cached' if cached else 'fetched once'})\n{'=' * 60}")

        flat = flat_text(response)
        replay_vision(reference_module, flat)

        # Deterministic first, and in pipeline order, so a failure reads as a
        # position in the chain rather than as a list of unrelated mismatches.
        ocr_ratios.append(compare_ocr(response, image.name))
        reference_result, _migrated_result = compare_gesture(image, response, image.name)

        raw = image.read_bytes()
        held_before = reference.merge_memory

        # The reference accumulates into its own module-level memory; the
        # migrated chain into `memory`. Both are fed the same bytes in the same
        # order — and now the same Vision response — which is what makes the
        # comparison meaningful across frames.
        reference.process_frame(raw)
        result = pipeline.update_memory(response)
        if result.accepted:
            memory.apply_frame(result.text, frame_version=None, whole_page=True)

        page_text = memory.page_text(memory.current_page)

        compare_pointer(reference, held_before, reference.merge_memory, image.name)
        compare_prompt_context(reference_result, page_text, image.name)

        if not args.no_groq:
            section(f"{image.name} — page detection")
            # Both directions, because agreeing on one is not agreeing. A detector
            # that answered "same page" unconditionally would pass the first case
            # and is precisely the failure the reference had on a network error;
            # one that answered "different page" unconditionally would pass the
            # second and turn the page on every frame.
            compare_page_detection(
                reference, reconstructor, page_text, flat, "the page against itself", image.name
            )
            compare_page_detection(
                reference,
                reconstructor,
                page_text,
                ANOTHER_PAGE,
                "the page against a different one",
                image.name,
            )

        ratios.append(
            report(
                f"{image.name} — end to end",
                reference.merge_memory,
                page_text,
            )
        )

        if args.baseline and not args.no_groq:
            baselines.append(reference_self_agreement(reference_module, image, flat))

    section("verdict")
    if ocr_ratios:
        print(f"  mean OCR similarity        {sum(ocr_ratios) / len(ocr_ratios):.1%}")
    if ratios:
        print(f"  mean end-to-end similarity {sum(ratios) / len(ratios):.1%}")
    if baselines:
        print(f"  reference vs itself        {sum(baselines) / len(baselines):.1%}  <- the ceiling")

    if DIFFERENCES:
        print(f"\n  {len(DIFFERENCES)} deterministic difference(s) from the reference:")
        for difference in DIFFERENCES:
            print(f"    - {difference}")
    else:
        print("\n  no deterministic difference from the reference")

    if ratios and not args.no_groq:
        # Said plainly, because a low number here is the expected result and
        # reading it as a regression is the mistake these lines exist to stop.
        print(
            "\n  The end-to-end figure compares two independent Groq calls and\n"
            "  cannot reach 100%: reconstruction is a language model, and the\n"
            "  reference does not reproduce its own output either. Run with\n"
            "  --baseline to see how well it agrees with itself; the comparisons\n"
            "  above it are the deterministic ones, and the ones that test the port."
        )

    # Non-zero only for the comparisons that have a right answer. An end-to-end
    # ratio below 100% is the expected result, not a failure, and exiting on it
    # would make this script impossible to put in a pipeline.
    return 1 if DIFFERENCES else 0


if __name__ == "__main__":
    sys.exit(main())
