"""Single-transaction coordinator for the first OCR testing milestone."""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Protocol

import cv2
import numpy as np

logger = logging.getLogger(__name__)


class PipelineBusyError(RuntimeError):
    """Raised rather than buffering a second image transaction."""


class ImageValidationError(RuntimeError):
    """Raised when an image body violates endpoint constraints."""


class ImageWriteError(OSError):
    """Raised when latest.jpg cannot be overwritten."""


class ImagePreprocessorPort(Protocol):
    """Minimal preprocessing behavior required by the transaction."""

    def preprocess(self, image_bytes: bytes, output_path: Path) -> bytes: ...


class OcrProcessorPort(Protocol):
    """Minimal OCR behavior required by the transaction."""

    def extract_text(self, image_bytes: bytes) -> str: ...


@dataclass(frozen=True)
class PipelineResult:
    text: str
    characters: int
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
        minimum_width: int = 320,
        minimum_height: int = 240,
    ) -> None:
        self.preprocessor = preprocessor
        self.ocr_processor = ocr_processor
        self.latest_image_path = latest_image_path
        self.processed_image_path = processed_image_path
        self.output_path = output_path
        self.max_image_bytes = max_image_bytes
        self.minimum_width = minimum_width
        self.minimum_height = minimum_height
        self._busy = False

    def process(self, image_bytes: bytes) -> PipelineResult:
        if self._busy:
            raise PipelineBusyError("An OCR request is already in progress")
        self._busy = True
        started = perf_counter()
        try:
            width, height = self._validate(image_bytes)
            logger.info(
                "Image validated | size=%d bytes | resolution=%dx%d",
                len(image_bytes),
                width,
                height,
            )
            self.latest_image_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                self.latest_image_path.write_bytes(image_bytes)
            except OSError as exc:
                raise ImageWriteError(f"Could not save image: {exc}") from exc
            if not self.latest_image_path.is_file():
                raise ImageWriteError("Saved image does not exist")
            logger.info("Image saved | path=%s", self.latest_image_path)

            logger.info("Preprocessing started")
            processed = self.preprocessor.preprocess(image_bytes, self.processed_image_path)
            logger.info("Preprocessing completed | path=%s", self.processed_image_path)

            logger.info("OCR started")
            text = self.ocr_processor.extract_text(processed)
            logger.info("OCR completed")
            logger.info("Characters extracted | count=%d", len(text))

            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now(timezone.utc).isoformat()
            output = (
                "====================================\n"
                "Image\n"
                "latest.jpg\n"
                "Time\n"
                f"{timestamp}\n"
                "====================================\n"
                "Extracted Text\n"
                f"{text}"
            )
            try:
                self.output_path.write_text(output, encoding="utf-8")
            except OSError as exc:
                raise OutputWriteError(f"Could not write OCR output: {exc}") from exc
            logger.info("TXT written | path=%s", self.output_path)
            logger.info("Processing finished")
            return PipelineResult(
                text,
                len(text),
                self.latest_image_path,
                self.processed_image_path,
                self.output_path,
            )
        except Exception:
            logger.exception("OCR transaction stopped at the failing step")
            raise
        finally:
            logger.info("Execution time | seconds=%.3f", perf_counter() - started)
            self._busy = False

    def _validate(self, image_bytes: bytes) -> tuple[int, int]:
        if not image_bytes:
            raise ImageValidationError("The request body is empty")
        if len(image_bytes) > self.max_image_bytes:
            raise ImageValidationError(
                f"Image exceeds the {self.max_image_bytes}-byte limit"
            )
        if not image_bytes.startswith(b"\xff\xd8") or not image_bytes.endswith(b"\xff\xd9"):
            raise ImageValidationError("Uploaded file is not a valid JPEG")

        image = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ImageValidationError("JPEG image is unreadable")
        height, width = image.shape[:2]
        if width < self.minimum_width or height < self.minimum_height:
            raise ImageValidationError(
                f"Image resolution must be at least {self.minimum_width}x{self.minimum_height}"
            )
        return width, height


class OutputWriteError(OSError):
    """Raised when output.txt cannot be overwritten."""
