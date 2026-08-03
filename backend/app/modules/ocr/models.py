"""OCR module request and response contracts.

Two word shapes exist here on purpose, and the difference matters.

`backend.app.models.OCRWord` is the domain contract: text, confidence, and an
optional box, in the vocabulary the API and the database speak. It is what
leaves the backend.

`RecognizedWord` below is what an OCR provider actually produces: pixel geometry
plus the line and paragraph it belongs to. The word-selection algorithm needs all
of it — it scores candidates on vertical distance, horizontal offset, pointing
direction and box overlap, none of which survive a conversion to the domain
model. Dropping the geometry at the provider boundary and trying to recover it
later is what would force a second, parallel OCR representation.

So providers return `RecognizedWord`, the gesture pipeline consumes it, and
`to_domain()` converts once at the edge where the domain contract is required.
One pipeline, one word model inside it, one conversion out.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.app.models import BoundingBox, OCRPage, OCRParagraph, OCRWord


class ProcessedImage(BaseModel):
    """Provider-neutral reference to image data prepared by OpenCV."""

    frame_reference: str
    image_reference: str | None = None
    content: bytes | None = None
    content_type: str = "image/jpeg"


class OcrProcessRequest(BaseModel):
    """Input contract for a processed image awaiting OCR."""

    image: ProcessedImage
    detection_type: Literal["DOCUMENT_TEXT_DETECTION"] = "DOCUMENT_TEXT_DETECTION"


class OcrProcessResponse(BaseModel):
    """Normalized OCR output contract."""

    status: str = "pending"
    pages: list[OCRPage] = Field(default_factory=list)


class RecognizedWord(BaseModel):
    """One word as an OCR provider saw it, with the geometry kept.

    Frozen because several consumers hold the same word list and word selection
    reorders and rescores candidates; a mutable word would let one consumer's
    scoring pass corrupt another's copy.

    `bbox` is `(x_min, y_min, x_max, y_max)` in pixels. Centres are stored rather
    than derived because selection recomputes them for every candidate on every
    frame, and the provider already knows them.

    The three indices default to `-1` meaning "the provider did not say". Paddle
    reports per-line boxes with no paragraph structure, so `-1` is the honest
    answer there rather than a fabricated zero — and selection treats it as
    unknown instead of as the first paragraph.
    """

    model_config = ConfigDict(frozen=True)

    text: str
    bbox: tuple[int, int, int, int]
    center_x: float
    center_y: float
    confidence: float = 1.0
    word_index: int = -1
    line_index: int = -1
    paragraph_index: int = -1

    @classmethod
    def from_bbox(
        cls,
        text: str,
        bbox: tuple[int, int, int, int],
        *,
        confidence: float = 1.0,
        word_index: int = -1,
        line_index: int = -1,
        paragraph_index: int = -1,
    ) -> "RecognizedWord":
        """Build a word from its box, deriving the centre.

        Every provider carried its own copy of this arithmetic. Centralised so a
        provider cannot disagree with the others about where a word's middle is.
        """

        x_min, y_min, x_max, y_max = bbox
        return cls(
            text=text,
            bbox=(int(x_min), int(y_min), int(x_max), int(y_max)),
            center_x=(x_min + x_max) / 2.0,
            center_y=(y_min + y_max) / 2.0,
            confidence=confidence,
            word_index=word_index,
            line_index=line_index,
            paragraph_index=paragraph_index,
        )

    @property
    def height(self) -> float:
        return float(self.bbox[3] - self.bbox[1])

    @property
    def width(self) -> float:
        return float(self.bbox[2] - self.bbox[0])

    def to_domain(self) -> OCRWord:
        """Convert to the domain contract, dropping the pixel geometry.

        Confidence is clamped because the domain model constrains it to 0..1, and
        a provider reporting a score a hair outside that range should not raise a
        validation error in the middle of a reading session.
        """

        x_min, y_min, x_max, y_max = self.bbox
        return OCRWord(
            text=self.text,
            confidence=min(1.0, max(0.0, self.confidence)),
            bounding_box=BoundingBox(
                x=float(x_min),
                y=float(y_min),
                width=float(max(0, x_max - x_min)),
                height=float(max(0, y_max - y_min)),
            ),
        )


def words_to_page(
    words: list[RecognizedWord],
    *,
    page_number: int = 1,
    width: int | None = None,
    height: int | None = None,
) -> OCRPage:
    """Group recognised words into the domain's page/paragraph shape.

    Grouping is by `paragraph_index`. Words the provider could not assign (`-1`)
    are collected into a single trailing paragraph rather than dropped — a page
    OCR read poorly still contains its words, and silently losing them would look
    downstream like a shorter page rather than a degraded one.
    """

    grouped: dict[int, list[RecognizedWord]] = {}
    for word in words:
        grouped.setdefault(word.paragraph_index, []).append(word)

    paragraphs: list[OCRParagraph] = []
    # `-1` sorts first numerically but means "unknown", so it is pushed last.
    for key in sorted(grouped, key=lambda index: (index < 0, index)):
        members = grouped[key]
        paragraphs.append(
            OCRParagraph(
                text=" ".join(word.text for word in members),
                words=[word.to_domain() for word in members],
            )
        )

    return OCRPage(page_number=page_number, paragraphs=paragraphs, width=width, height=height)