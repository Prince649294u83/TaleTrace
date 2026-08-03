"""The OCR engines, behind one port.

Migrated from the standalone `OCRandGESTURE/Gesture/ocr/` tree. The recognition
logic is preserved as written — Google Vision's hierarchical walk and Paddle's
per-line word splitting are the parts that were actually tuned against real
pages, and rewriting them would throw away the only thing that had been tested
on real books.

What changed is everything around that logic:

  - the three providers no longer each carry a private copy of the
    centre-point arithmetic; they build words through `RecognizedWord.from_bbox`
  - image coercion (array / bytes / path / URL) was duplicated in the Google
    provider only; it now lives in `coerce_to_jpeg_bytes` where Paddle can use it
  - a missing optional dependency is reported when the provider is selected,
    not at import, so a machine without PaddleOCR can still run the rest of
    the system

`get_provider` is the only function the runtime calls. Everything above it is an
implementation detail of "read this image".
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

                            words.append(
                                RecognizedWord.from_bbox(
                                    text,
                                    _vertices_to_bbox(word.get("boundingBox", {})),
                                    confidence=_word_confidence(word, symbols),
                                    word_index=word_index,
                                    line_index=line_index,
                                    paragraph_index=paragraph_index,
                                )
                            )
                            word_index += 1

                            # Vision marks line ends on the symbol, not the word.
                            if any(
                                symbol.get("property", {})
                                .get("detectedBreak", {})
                                .get("type")
                                in ("LINE_BREAK", "EOL_SURE_SPACE")
                                for symbol in symbols
                            ):
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


class PaddleOcrProvider:
    """Local OCR via RapidOCR (ONNX) or PaddleOCR, whichever imports.

    RapidOCR is preferred because it is the ONNX port and avoids the native
    Paddle DLL problems on Windows. Both report per-line boxes, so a line is
    split into words by character-count proportion — approximate geometry, but
    good enough for word selection, which scores on relative position.
    """

    provider_name = "paddle"

    def __init__(self, lang: str = "en") -> None:
        self._engine_type: str | None = None
        self._ocr: Any = None
        self._lang = lang

    def accepts(self, source: Any) -> bool:
        return np is not None and isinstance(source, np.ndarray)

    def _engine(self) -> Any:
        """Load the engine on first use.

        Deferred because both backends load model weights, which costs seconds
        and memory that a session using Google Vision should never pay.
        """

        if self._ocr is not None:
            return self._ocr

        try:
            from rapidocr_onnxruntime import RapidOCR

            self._ocr = RapidOCR()
            self._engine_type = "rapidocr"
            return self._ocr
        except ImportError:
            pass

        try:
            os.environ.setdefault("FLAGS_enable_pir_api", "0")
            os.environ.setdefault("FLAGS_use_mkldnn", "0")
            from paddleocr import PaddleOCR

            try:
                self._ocr = PaddleOCR(use_angle_cls=True, lang=self._lang)
            except TypeError:
                # Older/newer releases disagree about this kwarg.
                self._ocr = PaddleOCR(lang=self._lang)
            self._engine_type = "paddleocr"
            return self._ocr
        except Exception as error:
            raise OcrProviderUnavailable(
                f"No local OCR engine available ({error}). "
                "Install rapidocr-onnxruntime or paddleocr."
            ) from error

    def extract(self, source: Any) -> list[RecognizedWord]:
        engine = self._engine()

        if self._engine_type == "rapidocr":
            result, _ = engine(source)
            raw_lines = result or []
        else:
            results = engine.ocr(source)
            if not results or not results[0]:
                return []
            raw_lines = [[item[0], item[1][0], item[1][1]] for item in results[0]]

        words: list[RecognizedWord] = []
        for line_index, (box_points, text, confidence) in enumerate(
            (line[0], line[1], line[2]) for line in raw_lines
        ):
            text = (text or "").strip()
            if not text:
                continue

            xs = [point[0] for point in box_points]
            ys = [point[1] for point in box_points]
            x_min, x_max = int(min(xs)), int(max(xs))
            y_min, y_max = int(min(ys)), int(max(ys))

            tokens = text.split()
            if len(tokens) == 1:
                words.append(
                    RecognizedWord.from_bbox(
                        text,
                        (x_min, y_min, x_max, y_max),
                        confidence=float(confidence),
                        word_index=len(words),
                        line_index=line_index,
                    )
                )
                continue

            # Split the line box across its words by character share. Crude, but
            # word selection compares candidates against each other, so a
            # consistent bias costs nothing.
            total_chars = max(1, sum(len(token) for token in tokens))
            box_width = x_max - x_min
            cursor = x_min
            for token in tokens:
                token_width = int(box_width * len(token) / total_chars)
                words.append(
                    RecognizedWord.from_bbox(
                        token,
                        (cursor, y_min, cursor + token_width, y_max),
                        confidence=float(confidence),
                        word_index=len(words),
                        line_index=line_index,
                    )
                )
                cursor += token_width

        return words


class JsonOcrProvider:
    """Replays OCR output from a JSON file.

    Not a test double: it is how a recorded page is fed through the real
    pipeline deterministically, which is what makes the end-to-end scenarios
    runnable without a camera or an API key.
    """

    provider_name = "json"

    def accepts(self, source: Any) -> bool:
        return isinstance(source, (str, list))

    def extract(self, source: Any) -> list[RecognizedWord]:
        import json

        if isinstance(source, list):
            data = source
        else:
            if not os.path.exists(source):
                raise OcrProviderError(f"OCR JSON file not found: {source}")
            with open(source, encoding="utf-8") as handle:
                data = json.load(handle)

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


_PROVIDERS: dict[str, type] = {
    GoogleVisionProvider.provider_name: GoogleVisionProvider,
    PaddleOcrProvider.provider_name: PaddleOcrProvider,
    JsonOcrProvider.provider_name: JsonOcrProvider,
}


def get_provider(name: str | None = None, **kwargs: Any):
    """Resolve a provider by name, defaulting to `OCR_PROVIDER` then Google Vision.

    Returns the provider without probing it. Credentials are checked when a
    frame is actually submitted, so constructing the runtime never requires a
    network call.
    """

    resolved = (name or os.environ.get("OCR_PROVIDER") or GoogleVisionProvider.provider_name).lower()
    if resolved not in _PROVIDERS:
        raise OcrProviderUnavailable(
            f"Unknown OCR provider '{resolved}'. Available: {', '.join(sorted(_PROVIDERS))}"
        )
    return _PROVIDERS[resolved](**kwargs)


def _vertices_to_bbox(bounding: dict[str, Any]) -> tuple[int, int, int, int]:
    """Reduce a Vision bounding polygon to an axis-aligned box."""

    vertices = bounding.get("vertices") or bounding.get("normalizedVertices") or []
    if not vertices:
        return (0, 0, 0, 0)
    xs = [vertex.get("x", 0) for vertex in vertices]
    ys = [vertex.get("y", 0) for vertex in vertices]
    return (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys)))


def _word_confidence(word: dict[str, Any], symbols: list[dict[str, Any]]) -> float:
    """Vision reports confidence per word, per symbol, or not at all."""

    confidence = word.get("confidence")
    if confidence is not None:
        return float(confidence)
    scores = [s["confidence"] for s in symbols if s.get("confidence") is not None]
    return float(sum(scores) / len(scores)) if scores else 1.0
