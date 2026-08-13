"""The replay adapter: recorded Google Vision responses, no network.

This is **not** an OCR provider, and the production runtime cannot select it.
`get_ocr_engine()` returns Google Vision and nothing else; this class has to be
constructed explicitly, which is what keeps it out of production by
construction rather than by convention.

What it is for
--------------
Tests, demos, CI, offline development, and reproducing a bug deterministically.
It replaces exactly one thing — the network call to Google Vision — and leaves
every downstream stage running the same code as production:

    production   ESP32 -> OpenCV -> Google Vision -> parser -> Merge Engine -> ...
    replay       recorded response ------------->  parser -> Merge Engine -> ...

Only the source differs. That is the entire purpose of the seam: it is how a
refactor is shown to have preserved behaviour.

One parser, not two
-------------------
Replaying a recorded Vision response does **not** re-implement Vision parsing —
it hands the response to `GoogleVisionProvider.parse_response`, the same and only
parser production uses. If that parser changes, replay changes with it, which is
what makes replayed results trustworthy evidence about production.

The second accepted format is a plain list of `{text, bbox, ...}` dicts. That one
is a *fixture* format: it builds words from explicit geometry for tests that need
a specific layout (a word at a known point, two paragraphs at known heights).
It parses nothing Vision emits, so it is not a second parser — it is a way of
writing down a page by hand.
"""

from __future__ import annotations

import json
import os
from typing import Any

from backend.app.modules.ocr.models import RecognizedWord
from backend.app.modules.ocr.providers import GoogleVisionProvider, OcrProviderError


class ReplayAdapter:
    """Replays OCR results from a recorded Vision response or a word fixture.

    Accepts, in either case as an in-memory object or a path to a JSON file:

      - a recorded Google Vision API response (a dict with `responses`), which
        is routed through the production parser
      - a list of `{text, bbox, confidence, word_index, line_index,
        paragraph_index}` fixture dicts
    """

    # Satisfies the OCR port, which every source must, so the pipeline can name
    # it in an error. It is *not* a registry key: nothing maps a string to this
    # class, so the runtime has no way to ask for it. Being unreachable is a
    # property of there being no lookup, not of hiding the name.
    provider_name = "replay"

    def accepts(self, source: Any) -> bool:
        return isinstance(source, (str, list, dict))

    def extract(self, source: Any) -> list[RecognizedWord]:
        data = self._load(source)

        # A recorded Vision response goes through the production parser.
        if isinstance(data, dict):
            return GoogleVisionProvider.parse_response(data)

        if not isinstance(data, list):
            raise OcrProviderError(
                f"Replay source must be a recorded Vision response or a word list, "
                f"got {type(data).__name__}"
            )

        words: list[RecognizedWord] = []
        for item in data:
            text = item.get("text", "")
            bbox = item.get("bbox", [])
            if not text or len(bbox) != 4:
                continue
            words.append(
                RecognizedWord.from_bbox(
                    text,
                    tuple(bbox),  # type: ignore[arg-type]
                    confidence=item.get("confidence", 1.0),
                    word_index=item.get("word_index", -1),
                    line_index=item.get("line_index", -1),
                    paragraph_index=item.get("paragraph_index", -1),
                )
            )
        return words

    @staticmethod
    def _load(source: Any) -> Any:
        if isinstance(source, (list, dict)):
            return source
        if not isinstance(source, str):
            raise OcrProviderError(f"Unsupported replay source: {type(source).__name__}")
        if not os.path.exists(source):
            raise OcrProviderError(f"Replay file not found: {source}")
        with open(source, encoding="utf-8") as handle:
            return json.load(handle)
