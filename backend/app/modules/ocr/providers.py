"""OCR providers: OCR.Space (prototype) and Google Vision (fallback).

The prototype reads pages with OCR.Space Engine 2, validated end-to-end through
the gesture pipeline on a real pointing photograph.  Google Vision is the
fallback when the OCR.Space key is absent but a Vision key is available.

    ESP32 -> OpenCV -> OCR.Space Engine 2 -> parser -> Merge Engine -> Merge Memory
    (fallback)        Google Vision

Provider selection is configuration-level only: ``get_ocr_engine`` looks at which
keys are set and picks one provider for the session.  A transient API failure
from OCR.Space does *not* trigger an automatic switch to Vision mid-session —
the pipeline retries or drops the frame, and the operator chooses the fallback
by clearing the OCR.Space key.

Google Vision’s recognition logic is preserved as written — the hierarchical
walk over Vision’s response is the part tuned against real pages.

Tests and demos use ``ocr/replay.py``, which feeds recorded Vision responses
through ``GoogleVisionProvider.parse_response`` — the same parser used here.
OCR.Space responses are replayed through ``OcrSpaceProvider.parse_response``.

What changed from the standalone
--------------------------------
  - the centre-point arithmetic is no longer a private copy per provider; words
    are built through `RecognizedWord.from_bbox`
  - image coercion (array / bytes / path / URL) lives in `coerce_to_jpeg_bytes`
  - a missing credential is reported when a frame is submitted, not at import,
    so the app boots without a key
"""

from __future__ import annotations

import base64
import os
from typing import Any

from backend.app.modules.ocr.models import RecognizedWord
from backend.app.shared.exceptions import TaleTraceError

# Providers are optional at import time. A dev box with no PaddleOCR wheel, or a
# CI runner with no OpenCV, must still be able to import the OCR module and use
# the JSON provider — the failure belongs at selection, where it names the thing
# that is actually missing.
try:  # pragma: no cover - exercised by absence, not by tests
    import numpy as np
except ImportError:  # pragma: no cover
    np = None  # type: ignore[assignment]

try:  # pragma: no cover
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None  # type: ignore[assignment]


class OcrProviderError(TaleTraceError):
    """An OCR engine could not produce a result.

    Distinct from "the page was blank": callers retry this and do not advance
    the session on it.
    """


class OcrProviderUnavailable(OcrProviderError):
    """The requested provider cannot run here — missing dependency or credential."""


def coerce_to_jpeg_bytes(source: Any, *, timeout: float = 10.0) -> bytes:
    """Turn any supported image reference into JPEG bytes.

    Accepts a BGR array, raw bytes, an HTTP(S) URL, or a filesystem path. Bytes
    are passed through untouched rather than re-encoded: the ESP32 already sends
    JPEG, and a decode/encode round trip would cost quality for nothing.
    """

    if isinstance(source, bytes):
        return source

    if np is not None and isinstance(source, np.ndarray):
        if cv2 is None:  # pragma: no cover
            raise OcrProviderUnavailable("OpenCV is required to encode an image array")
        success, encoded = cv2.imencode(".jpg", source)
        if not success:
            raise OcrProviderError("Failed to encode image array to JPEG")
        return encoded.tobytes()

    if isinstance(source, str):
        if source.startswith(("http://", "https://")):
            import requests

            response = requests.get(source, timeout=timeout)
            if response.status_code != 200:
                raise OcrProviderError(
                    f"Failed to fetch image from {source}: HTTP {response.status_code}"
                )
            return response.content
        if not os.path.exists(source):
            raise OcrProviderError(f"Image file not found: {source}")
        with open(source, "rb") as handle:
            return handle.read()

    raise OcrProviderError(f"Unsupported image source type: {type(source).__name__}")


class GoogleVisionProvider:
    """Google Cloud Vision DOCUMENT_TEXT_DETECTION over the REST API.

    The only provider that reports paragraph structure, which is why it is the
    default: paragraph indices are what let Merge Memory rebuild a page's shape
    rather than one long run of words.
    """

    provider_name = "google_vision"

    def __init__(self, api_key: str | None = None, *, timeout: float = 15.0) -> None:
        self.api_key = api_key if api_key is not None else os.environ.get("GOOGLE_VISION_API_KEY", "")
        self.timeout = timeout

    def accepts(self, source: Any) -> bool:
        return isinstance(source, (bytes, str)) or (np is not None and isinstance(source, np.ndarray))

    def extract(self, source: Any) -> list[RecognizedWord]:
        if not self.api_key:
            raise OcrProviderUnavailable(
                "GOOGLE_VISION_API_KEY is not set; cannot use the Google Vision provider"
            )

        import requests

        payload = {
            "requests": [
                {
                    "image": {"content": base64.b64encode(coerce_to_jpeg_bytes(source)).decode("utf-8")},
                    "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
                }
            ]
        }
        response = requests.post(
            f"https://vision.googleapis.com/v1/images:annotate?key={self.api_key}",
            json=payload,
            timeout=self.timeout,
        )
        if response.status_code != 200:
            raise OcrProviderError(
                f"Google Vision request failed (HTTP {response.status_code}): {response.text[:200]}"
            )
        return self.parse_response(response.json())

    @staticmethod
    def parse_response(results: dict[str, Any]) -> list[RecognizedWord]:
        """Walk responses -> fullTextAnnotation -> pages -> blocks -> paragraphs -> words.

        Falls back to the flat `textAnnotations` list when the caller used plain
        TEXT_DETECTION, which carries no paragraph structure — hence the `-1`
        indices on that path rather than invented ones.
        """

        if not results or not results.get("responses"):
            return []

        response = results["responses"][0]
        annotation = response.get("fullTextAnnotation")
        words: list[RecognizedWord] = []

        if annotation and "pages" in annotation:
            word_index = 0
            line_index = 0
            paragraph_index = 0

            for page in annotation.get("pages", []):
                for block in page.get("blocks", []):
                    for paragraph in block.get("paragraphs", []):
                        for word in paragraph.get("words", []):
                            symbols = word.get("symbols", [])
                            text = "".join(symbol.get("text", "") for symbol in symbols).strip()
                            if not text:
                                continue

                            break_type = _break_after(symbols)

                            words.append(
                                RecognizedWord.from_bbox(
                                    text,
                                    _vertices_to_bbox(word.get("boundingBox", {})),
                                    confidence=_word_confidence(word, symbols),
                                    word_index=word_index,
                                    line_index=line_index,
                                    paragraph_index=paragraph_index,
                                    space_after=break_type in _SPACING_BREAKS,
                                )
                            )
                            word_index += 1

                            # Vision marks line ends on the symbol, not the word.
                            if break_type in ("LINE_BREAK", "EOL_SURE_SPACE"):
                                line_index += 1

                        paragraph_index += 1
                        line_index += 1

            return words

        for index, annotation_item in enumerate(response.get("textAnnotations", [])[1:]):
            text = annotation_item.get("description", "").strip()
            if not text:
                continue
            words.append(
                RecognizedWord.from_bbox(
                    text,
                    _vertices_to_bbox(annotation_item.get("boundingPoly", {})),
                    confidence=float(annotation_item.get("confidence", 1.0)),
                    word_index=index,
                )
            )
        return words


def get_ocr_engine(**kwargs: Any) -> Any:
    """The OCR engine for this session.

    OCR.Space Engine 2 is the prototype provider, selected when
    ``OCR_SPACE_API_KEY`` is set.  Google Vision is the fallback, selected
    when only ``GOOGLE_VISION_API_KEY`` is set.  When neither key is present,
    OCR.Space is returned anyway so the error names the prototype key.

    Takes no ``OCR_PROVIDER`` variable: the selection is which key is set,
    not a string that could be misspelt.  The fallback is config-level:
    a transient OCR.Space failure does *not* switch to Vision mid-session.

    Returns the engine without probing it, so constructing the runtime never
    makes a network call. The credential is checked when a frame is submitted.
    """

    ocr_space_key = os.environ.get("OCR_SPACE_API_KEY", "").strip()
    vision_key = os.environ.get("GOOGLE_VISION_API_KEY", "").strip()

    if ocr_space_key:
        from backend.app.modules.ocr.ocr_space import OcrSpaceProvider

        return OcrSpaceProvider(api_key=ocr_space_key, **kwargs)

    if vision_key:
        return GoogleVisionProvider(api_key=vision_key, **kwargs)

    # No key at all — return OCR.Space so the error names the prototype key,
    # not the legacy one.
    from backend.app.modules.ocr.ocr_space import OcrSpaceProvider

    return OcrSpaceProvider(**kwargs)


def _vertices_to_bbox(bounding: dict[str, Any]) -> tuple[int, int, int, int]:
    """Reduce a Vision bounding polygon to an axis-aligned box."""

    vertices = bounding.get("vertices") or bounding.get("normalizedVertices") or []
    if not vertices:
        return (0, 0, 0, 0)
    xs = [vertex.get("x", 0) for vertex in vertices]
    ys = [vertex.get("y", 0) for vertex in vertices]
    return (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys)))


# The break types that mean a space follows the word. Vision distinguishes a
# space it is sure about from one it inferred, and a line end from a paragraph
# end, but for spacing purposes all four are the same answer. The ones
# deliberately absent are as important: `HYPHEN` ends a word split across a line
# and must close up, and no `detectedBreak` at all is what Vision reports between
# a quote and the word it hugs — which is exactly the case character classes
# cannot decide, because `"` opens and closes with the same character.
_SPACING_BREAKS = frozenset({"SPACE", "SURE_SPACE", "EOL_SURE_SPACE", "LINE_BREAK"})


def _break_after(symbols: list[dict[str, Any]]) -> str:
    """The break Vision detected after a word, or `""` if it detected none.

    Recorded on the last symbol of the word rather than on the word itself, so
    this reaches past the word to its final character. An absent property is
    returned as the empty string rather than None so callers can compare against
    a set of names without a null check.
    """

    if not symbols:
        return ""
    detected = symbols[-1].get("property", {}).get("detectedBreak", {})
    return str(detected.get("type", "") or "")


def _word_confidence(word: dict[str, Any], symbols: list[dict[str, Any]]) -> float:
    """Vision reports confidence per word, per symbol, or not at all."""

    confidence = word.get("confidence")
    if confidence is not None:
        return float(confidence)
    scores = [s["confidence"] for s in symbols if s.get("confidence") is not None]
    return float(sum(scores) / len(scores)) if scores else 1.0
