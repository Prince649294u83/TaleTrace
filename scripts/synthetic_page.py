"""Synthetic book pages, and the Vision responses that truthfully describe them.

    python -m scripts.synthetic_page                      # 4 pages, default location
    python -m scripts.synthetic_page --pages 12
    python -m scripts.synthetic_page --out somewhere/ --force

Why this exists
---------------
Every offline harness — `stress_session`, `validate_corpus --cached-only`,
Simulation Mode's `--offline` — replays recorded Google Vision responses instead
of calling the API. That makes them free to run and deterministic, and it makes
them **unrunnable on a checkout that has no photographs**, which is every
checkout but the one machine where the photographs happen to live.

So this generates both halves of the pair: a page image, and the Vision response
for *that* image, written into the same cache a real call would have populated.
From then on the offline harnesses cannot tell the difference, because there is
no difference in the thing they read.

The response is not invented text bolted onto an unrelated picture. Each word is
drawn at a measured position and that measurement becomes its bounding box, so
the fingertip-to-word mapping, the reading order and the paragraph structure are
all consistent with the pixels. A response whose boxes disagreed with the image
would quietly make every pointer assertion downstream meaningless.

What it is not
--------------
Not a substitute for photographs, and it must not be used as one. It has no
camera noise, no page curl, no shadow, no perspective and no motion blur, so it
can say nothing about whether OCR *reads* a real page — that is exactly the
question `validate_corpus` needs the user's real dataset to answer. What it is
good for is everything downstream of OCR: pointer arithmetic, Merge Memory
accumulation, queue behaviour and session stability, which are the same code
whatever the photograph looked like.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.modules.ocr.vision_cache import cache_path, write_cached  # noqa: E402
from scripts.vision_cache import preprocess  # noqa: E402

# Deliberately dull prose. The stress harness measures accumulation and drift, so
# the text needs paragraph structure and stable word counts, not interest. Each
# page is a list of paragraphs; each paragraph is a list of lines already broken
# to a width that fits, because breaking them here keeps the drawn position and
# the reported bounding box derived from the same loop.
PAGES: tuple[tuple[tuple[str, ...], ...], ...] = (
    (
        (
            "The lighthouse had stood on the point for eighty years,",
            "and in that time the sea had taken three boats, one pier,",
            "and the better part of the cliff path.",
        ),
        (
            "Martha climbed it every evening regardless of weather.",
            "The keeper's log recorded her visits in a hand that grew",
            "less certain each winter, until the entries stopped.",
        ),
    ),
    (
        (
            "What the log did not record was the reason for the climb.",
            "Two hundred and forty steps is a considerable price to pay",
            "for a view one has already memorised.",
        ),
        (
            "She went because the light needed turning, and because",
            "nobody else in the village would admit that it did.",
        ),
        (
            "The mechanism was older than the building it turned in.",
            "It required oil, patience, and a particular kind of",
            "stubbornness that the parish had in short supply.",
        ),
    ),
    (
        (
            "In the spring the inspector came, as inspectors do,",
            "with a clipboard and a theory about automation.",
        ),
        (
            "He explained that the light could be run from the mainland",
            "at a fraction of the cost, and that the steps would then",
            "be nobody's concern at all.",
        ),
        (
            "Martha listened to the whole of it without interrupting,",
            "which he mistook for agreement.",
        ),
    ),
    (
        (
            "The automation arrived in October and failed in November,",
            "during the first storm worth the name.",
        ),
        (
            "The mainland relay had been installed by a contractor who",
            "had never seen the point in weather, and the cable ran",
            "along the cliff path that the sea had been eating since",
            "before either of them was born.",
        ),
        (
            "Martha climbed the two hundred and forty steps that night",
            "and turned the light by hand until morning.",
        ),
    ),
)

FONT = cv2.FONT_HERSHEY_SIMPLEX
FONT_SCALE = 0.62
THICKNESS = 1
MARGIN_X = 70
MARGIN_TOP = 90
LINE_HEIGHT = 34
PARAGRAPH_GAP = 22
SPACE_WIDTH = 9
PAGE_WIDTH = 800
PAGE_HEIGHT = 1000
# Off-white with black ink. A pure-white background would be the one thing a
# photograph never is, and CLAHE — which every image passes through before Vision
# — behaves differently on a flat histogram than on a real one.
PAPER = 246
INK = 30


def render_page(paragraphs: tuple[tuple[str, ...], ...]) -> tuple[np.ndarray, list[list[dict]]]:
    """Draw one page. Returns the image and the words, grouped by paragraph.

    Position is measured with `getTextSize` and used for *both* the drawing and
    the reported box, so the two cannot disagree — which is the whole point of
    generating the pair together rather than writing the response by hand.
    """

    image = np.full((PAGE_HEIGHT, PAGE_WIDTH, 3), PAPER, dtype=np.uint8)
    grouped: list[list[dict]] = []
    y = MARGIN_TOP

    for lines in paragraphs:
        words: list[dict] = []
        for line_number, line in enumerate(lines):
            tokens = line.split()
            x = MARGIN_X
            for position, word in enumerate(tokens):
                (width, height), _baseline = cv2.getTextSize(word, FONT, FONT_SCALE, THICKNESS)
                cv2.putText(image, word, (x, y), FONT, FONT_SCALE, (INK, INK, INK), THICKNESS, cv2.LINE_AA)

                # The break Vision would have detected after this word. Not
                # decoration: the parser reads `space_after` from it, and a word
                # with no break at all is treated as running into the next one —
                # which collapses the whole page into a single unspaced token and
                # makes every word count downstream wrong.
                last_of_line = position == len(tokens) - 1
                last_of_paragraph = last_of_line and line_number == len(lines) - 1
                if last_of_paragraph:
                    break_type = "LINE_BREAK"
                elif last_of_line:
                    break_type = "EOL_SURE_SPACE"
                else:
                    break_type = "SPACE"

                # `y` is the baseline, so the box top is `y - height`. Getting this
                # backwards would put every box below its word and send the
                # fingertip mapping to the following line.
                words.append(
                    {"text": word, "box": (x, y - height, x + width, y), "break": break_type}
                )
                x += width + SPACE_WIDTH
            y += LINE_HEIGHT
        grouped.append(words)
        y += PARAGRAPH_GAP

    return image, grouped


def vision_response(grouped: list[list[dict]]) -> dict:
    """The Google Vision response this page would have produced.

    Shaped as `responses -> fullTextAnnotation -> pages -> blocks -> paragraphs`,
    the nested form the production parser prefers, with one Vision paragraph per
    rendered paragraph so Merge Memory's segmenter sees the structure that was
    actually drawn.
    """

    paragraphs = []
    for words in grouped:
        entries = []
        for word in words:
            left, top, right, bottom = word["box"]
            entries.append(
                {
                    "symbols": [{"text": char} for char in word["text"][:-1]]
                    + [
                        {
                            "text": word["text"][-1],
                            # Vision records the break on the word's *last symbol*,
                            # not on the word, and the parser reaches past the word
                            # to find it. Putting it anywhere else is the same as
                            # omitting it.
                            "property": {"detectedBreak": {"type": word["break"]}},
                        }
                    ],
                    "boundingBox": {
                        "vertices": [
                            {"x": left, "y": top},
                            {"x": right, "y": top},
                            {"x": right, "y": bottom},
                            {"x": left, "y": bottom},
                        ]
                    },
                    "confidence": 0.99,
                }
            )
        paragraphs.append({"words": entries})

    return {
        "responses": [
            {"fullTextAnnotation": {"pages": [{"blocks": [{"paragraphs": paragraphs}]}]}}
        ]
    }


def generate(out_dir: Path, *, count: int, force: bool) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = 0

    for index in range(count):
        # Cycles if more pages are asked for than there is prose. The stress
        # harness wants length, and a repeated page is a reader re-reading a
        # chapter — which is a case Merge Memory has to handle anyway.
        paragraphs = PAGES[index % len(PAGES)]
        image, grouped = render_page(paragraphs)

        path = out_dir / f"page_{index + 1:02d}.png"
        ok, encoded = cv2.imencode(".png", image)
        if not ok:
            print(f"  could not encode {path.name}")
            continue
        path.write_bytes(encoded.tobytes())

        # Keyed on the *preprocessed* bytes, through the pipeline's own `_enhance`,
        # because that is what a live call would have been given and therefore what
        # the offline lookup will hash.
        prepared = preprocess(path.read_bytes())
        if force or not cache_path(prepared).exists():
            write_cached(prepared, vision_response(grouped))

        words = sum(len(w) for w in grouped)
        print(f"  {path.name}  {len(grouped)} paragraphs, {words} words")
        written += 1

    print(f"\n  {written} page(s) in {out_dir}")
    print(f"  Vision responses cached alongside the real ones in {cache_path(b'').parent}")
    print("\n  These are generated, not photographed — good for stability and pointer")
    print("  work, worthless for judging whether OCR can read a real page.")
    return 0 if written else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out",
        default=str(ROOT / ".taletrace_cache" / "synthetic"),
        help="where to write the pages (default: .taletrace_cache/synthetic)",
    )
    parser.add_argument("--pages", type=int, default=4, help="how many pages to generate")
    parser.add_argument(
        "--force", action="store_true", help="rewrite cached responses that already exist"
    )
    args = parser.parse_args(argv)

    return generate(Path(args.out).expanduser(), count=max(1, args.pages), force=args.force)


if __name__ == "__main__":
    sys.exit(main())
