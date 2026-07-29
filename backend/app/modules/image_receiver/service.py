"""Single-transaction coordinator for the first OCR testing milestone."""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)


class PipelineBusyError(RuntimeError):
    """Raised rather than buffering a second image transaction."""


class ImageValidationError(RuntimeError):
    """Raised when an image body violates endpoint constraints."""


class ImagePreprocessorPort(Protocol):
    """Minimal preprocessing behavior required by the transaction."""

    def preprocess(self, image_bytes: bytes, output_path: Path) -> bytes: ...


class OcrProcessorPort(Protocol):
    """Minimal OCR behavior required by the transaction."""

    def extract_text(self, image_bytes: bytes) -> str: ...


@dataclass(frozen=True)
class PipelineResult:
    text: str
    latest_image: Path
    processed_image: Path
    output_file: Path


class SingleImageOcrPipeline:
    """Run save, preprocess, OCR, and text persistence inline."""

    def __init__(
        self,
        preprocessor: ImagePreprocessorPort,
        ocr_processor: OcrProcessorPort,
        latest_image_path: Path,
        processed_image_path: Path,
        output_path: Path,
        max_image_bytes: int,
    ) -> None:
        self.preprocessor = preprocessor
        self.ocr_processor = ocr_processor
        self.latest_image_path = latest_image_path
        self.processed_image_path = processed_image_path
        self.output_path = output_path
        self.max_image_bytes = max_image_bytes
        self._busy = False

    def process(self, image_bytes: bytes) -> PipelineResult:
        if self._busy:
            raise PipelineBusyError("An OCR request is already in progress")
        self._busy = True
        try:
            self._validate(image_bytes)
            logger.info(
                "Step 1/4: saving %d-byte image to %s",
                len(image_bytes),
                self.latest_image_path,
            )
            self.latest_image_path.parent.mkdir(parents=True, exist_ok=True)
            self.latest_image_path.write_bytes(image_bytes)

            logger.info(
                "Step 2/4: preprocessing image to %s", self.processed_image_path
            )
            processed = self.preprocessor.preprocess(image_bytes, self.processed_image_path)

            logger.info("Step 3/4: requesting DOCUMENT_TEXT_DETECTION")
            text = self.ocr_processor.extract_text(processed)

            logger.info("Step 4/4: overwriting OCR text at %s", self.output_path)
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            self.output_path.write_text(text, encoding="utf-8")
            logger.info("OCR transaction completed successfully")
            return PipelineResult(
                text, self.latest_image_path, self.processed_image_path, self.output_path
            )
        except Exception:
            logger.exception("OCR transaction stopped at the failing step")
            raise
        finally:
            self._busy = False

    def _validate(self, image_bytes: bytes) -> None:
        if not image_bytes:
            raise ImageValidationError("The request body is empty")
        if len(image_bytes) > self.max_image_bytes:
            raise ImageValidationError(
                f"Image exceeds the {self.max_image_bytes}-byte limit"
            )