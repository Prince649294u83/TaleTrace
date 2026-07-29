"""OpenCV preprocessing for the synchronous OCR testing milestone."""

from pathlib import Path

import cv2
import numpy as np


class ImagePreprocessingError(RuntimeError):
    """Raised when an uploaded image cannot be prepared for OCR."""


class ImagePreprocessor:
    """Lightly clean and normalize a JPEG without changing its geometry."""

    def preprocess(self, image_bytes: bytes, output_path: Path) -> bytes:
        image = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ImagePreprocessingError("Image is unreadable")

        denoised = cv2.GaussianBlur(image, (3, 3), 0)
        lab = cv2.cvtColor(denoised, cv2.COLOR_BGR2LAB)
        lightness, channel_a, channel_b = cv2.split(lab)
        normalized_lightness = cv2.createCLAHE(clipLimit=1.5, tileGridSize=(8, 8)).apply(
            lightness
        )
        processed = cv2.cvtColor(
            cv2.merge((normalized_lightness, channel_a, channel_b)), cv2.COLOR_LAB2BGR
        )

        encoded, buffer = cv2.imencode(
            ".jpg", processed, [int(cv2.IMWRITE_JPEG_QUALITY), 95]
        )
        if not encoded:
            raise ImagePreprocessingError("OpenCV could not encode the processed image")

        processed_bytes = buffer.tobytes()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            output_path.write_bytes(processed_bytes)
        except OSError as exc:
            raise ImagePreprocessingError(
                f"Could not write processed image: {exc}"
            ) from exc
        return processed_bytes
