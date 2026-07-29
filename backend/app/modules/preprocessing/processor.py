"""Deterministic preprocessing for the OCR testing milestone."""

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter, ImageOps, UnidentifiedImageError


class ImagePreprocessingError(RuntimeError):
    """Raised when an uploaded image cannot be prepared for OCR."""


class ImagePreprocessor:
    """Convert an image to a high-contrast, sharpened grayscale JPEG."""

    def preprocess(self, image_bytes: bytes, output_path: Path) -> bytes:
        try:
            with Image.open(BytesIO(image_bytes)) as source:
                source.verify()
            with Image.open(BytesIO(image_bytes)) as source:
                image = ImageOps.exif_transpose(source).convert("L")
                image = ImageOps.autocontrast(image)
                image = ImageEnhance.Contrast(image).enhance(1.5)
                image = image.filter(ImageFilter.SHARPEN)

                buffer = BytesIO()
                image.save(buffer, format="JPEG", quality=95, optimize=True)
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise ImagePreprocessingError(f"Invalid or unreadable image: {exc}") from exc

        processed_bytes = buffer.getvalue()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(processed_bytes)
        return processed_bytes