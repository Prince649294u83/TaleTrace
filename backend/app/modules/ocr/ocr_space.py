"""OCR.Space Engine 2: the prototype OCR provider.

Validated end-to-end on a real pointing photograph: image -> word bounding
boxes -> fingertip (577, 796) -> Gesture Engine -> "impact" -> containing
sentence.  That chain proves OCR.Space gives the spatial precision the gesture
pipeline needs, and it is what justifies using it as the primary provider while
the Google Vision key is unavailable.

    ESP32 -> preprocessing -> OCR.Space Engine 2 -> parser -> Merge Engine -> Merge Memory

The same contract as Google Vision: ``extract`` returns ``list[RecognizedWord]``
through ``from_bbox``, and the pipeline cannot tell which provider ran.

Rate limits (free tier, per ocr.space/ocrapi)
---------------------------------------------
  - 500 requests per day per IP
  - 25 000 requests per month
  - 1 MB per image

The live session throttles frames before they reach any provider, so the daily
cap is an upstream concern — not something this adapter enforces.  The 1 MB
limit *is* enforced here because a frame that exceeds it will be rejected by
the API and the request is wasted.

Paragraph grouping
------------------
OCR.Space reports Lines and Words but has no concept of paragraphs.  Paragraph
boundaries are inferred from vertical gaps: when the gap between consecutive
lines exceeds ``PARAGRAPH_GAP_RATIO`` times the median line height, a new
paragraph begins.  The constant is named and exposed so tests can lock the
current heuristic while future tuning remains a one-line change.

A two-column page will group incorrectly under this heuristic.  That is a known
limitation of the prototype provider, accepted because the demo material is
single-column.
"""

from __future__ import annotations

import os
from typing import Any

from backend.app.modules.ocr.models import RecognizedWord
from backend.app.modules.ocr.providers import (
    OcrProviderError,
    OcrProviderUnavailable,
    coerce_to_jpeg_bytes,
)

try:  # pragma: no cover - exercised by absence, not by tests
    import numpy as np
except ImportError:  # pragma: no cover
    np = None  # type: ignore[assignment]


# The vertical-gap multiplier that decides where one paragraph ends and another
# begins.  Lines separated by more than this many median-line-heights are placed
# in separate paragraphs.  Named so tests can reference it and so a future page
# layout that needs a different threshold can change one number.
PARAGRAPH_GAP_RATIO = 1.5

# OCR.Space free-tier image size limit, in bytes.
_MAX_IMAGE_BYTES = 1_048_576  # 1 MB


class OcrSpaceProvider:
    """OCR.Space Engine 2 with word-coordinate overlays.

    The prototype provider for TaleTrace.  Satisfies the ``OcrProvider``
    protocol: ``provider_name``, ``accepts``, ``extract``.

    Credentials are checked at ``extract`` time, not at construction, so the
    app boots without a key — the same pattern ``GoogleVisionProvider`` uses.
    """

    provider_name = "ocr_space"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        engine: int = 2,
        timeout: float = 30.0,
    ) -> None:
        self.api_key = (
            api_key
            if api_key is not None
            else os.environ.get("OCR_SPACE_API_KEY", "")
        )
        self.engine = engine
        self.timeout = timeout

    def accepts(self, source: Any) -> bool:
        """Same inputs as Google Vision: bytes, ndarray, path, or URL."""

        return isinstance(source, (bytes, str)) or (
            np is not None and isinstance(source, np.ndarray)
        )

    def extract(self, source: Any) -> list[RecognizedWord]:
        """Send one image to OCR.Space and return recognised words.

        Raises ``OcrProviderUnavailable`` when the key is absent and
        ``OcrProviderError`` on any API or parsing failure.  Transient
        failures are raised, never silently swallowed — the pipeline
        retries or ignores the frame, but it is not this adapter's job
        to decide which.
        """

        if not self.api_key:
            raise OcrProviderUnavailable(
                "OCR_SPACE_API_KEY is not set; cannot use the OCR.Space provider"
            )

        image_bytes = coerce_to_jpeg_bytes(source)

        if len(image_bytes) > _MAX_IMAGE_BYTES:
            raise OcrProviderError(
                f"Image is {len(image_bytes):,} bytes — exceeds the OCR.Space "
                f"free-tier limit of {_MAX_IMAGE_BYTES:,} bytes (1 MB). "
                f"Reduce the resolution or JPEG quality before submitting."
            )

        import requests

        try:
            response = requests.post(
                "https://api.ocr.space/parse/image",
                files={"file": ("frame.jpg", image_bytes, "image/jpeg")},
                data={
                    "apikey": self.api_key,
                    "language": "eng",
                    "isOverlayRequired": True,
                    "OCREngine": self.engine,
                },
                timeout=self.timeout,
            )
        except requests.RequestException as error:
            raise OcrProviderError(
                f"OCR.Space request failed: {error}"
            ) from error

        if response.status_code != 200:
            raise OcrProviderError(
                f"OCR.Space returned HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )

        try:
            result = response.json()
        except ValueError as error:
            raise OcrProviderError(
                "OCR.Space returned invalid JSON"
            ) from error

        if result.get("IsErroredOnProcessing"):
            messages = result.get("ErrorMessage") or result.get("ErrorDetails") or "unknown error"
            raise OcrProviderError(f"OCR.Space processing error: {messages}")

        return self.parse_response(result)

    @staticmethod
    def parse_response(result: dict[str, Any]) -> list[RecognizedWord]:
        """Parse an OCR.Space JSON response into ``RecognizedWord`` objects.

        Accepts the complete API response (the dict with ``ParsedResults``).
        Exposed as a static method so the regression fixture can replay a
        recorded response without making an API call — the same pattern
        ``GoogleVisionProvider.parse_response`` uses for the replay adapter.
        """

        parsed_results = result.get("ParsedResults") or []
        if not parsed_results:
            return []

        overlay = parsed_results[0].get("TextOverlay")
        if not overlay:
            return []

        lines_data = overlay.get("Lines") or []
        if not lines_data:
            return []

        # -- First pass: collect every word with its line membership. ---------
        line_words: list[list[RecognizedWord]] = []
        line_y_centers: list[float] = []
        line_heights: list[float] = []
        global_word_index = 0

        for line_idx, line in enumerate(lines_data):
            words_in_line: list[RecognizedWord] = []

            for word_data in line.get("Words") or []:
                text = word_data.get("WordText", "")
                if not text:
                    continue

                left = word_data.get("Left", 0)
                top = word_data.get("Top", 0)
                width = word_data.get("Width", 0)
                height = word_data.get("Height", 0)

                bbox = (int(left), int(top), int(left + width), int(top + height))
                words_in_line.append(
                    RecognizedWord.from_bbox(
                        text=text,
                        bbox=bbox,
                        word_index=global_word_index,
                        line_index=line_idx,
                        # paragraph_index is assigned in the second pass
                        paragraph_index=-1,
                        space_after=True,
                    )
                )
                global_word_index += 1

            if words_in_line:
                line_words.append(words_in_line)
                # Use the first word's vertical centre as representative.
                first = words_in_line[0]
                line_y_centers.append(first.center_y)
                line_heights.append(float(first.bbox[3] - first.bbox[1]))

        if not line_words:
            return []

        # -- Second pass: assign paragraph indices by vertical gap. -----------
        paragraph_indices = _assign_paragraphs(line_y_centers, line_heights)

        all_words: list[RecognizedWord] = []
        for line_group, para_idx in zip(line_words, paragraph_indices):
            for word in line_group:
                # RecognizedWord is frozen, so rebuild with the real paragraph.
                all_words.append(
                    RecognizedWord.from_bbox(
                        text=word.text,
                        bbox=word.bbox,
                        confidence=word.confidence,
                        word_index=word.word_index,
                        line_index=word.line_index,
                        paragraph_index=para_idx,
                        space_after=word.space_after,
                    )
                )

        return all_words


def _assign_paragraphs(
    y_centers: list[float],
    heights: list[float],
) -> list[int]:
    """Group lines into paragraphs by vertical gap.

    A gap larger than ``PARAGRAPH_GAP_RATIO`` times the median line height
    starts a new paragraph.  Returns a list of paragraph indices aligned with
    the input lists.
    """

    if not y_centers:
        return []

    if len(y_centers) == 1:
        return [0]

    sorted_heights = sorted(heights)
    median_height = sorted_heights[len(sorted_heights) // 2]
    threshold = median_height * PARAGRAPH_GAP_RATIO

    paragraphs = [0]
    current_paragraph = 0

    for i in range(1, len(y_centers)):
        gap = abs(y_centers[i] - y_centers[i - 1])
        if gap > threshold:
            current_paragraph += 1
        paragraphs.append(current_paragraph)

    return paragraphs
