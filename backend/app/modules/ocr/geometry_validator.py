"""Page geometry validator and low-frequency background perceptual fingerprinting."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from backend.app.modules.gesture_engine.selection_models import CoordinateSpace, PageContext

logger = logging.getLogger(__name__)


class PageGeometryValidator:
    """Validates whether a captured frame is spatially and visually compatible with an active PageContext.

    To avoid expensive repeated OCR calls when a user's finger moves across the page,
    fingerprints are computed strictly on the background `page_region` with the dynamic
    `gesture_region` masked out.
    """

    @staticmethod
    def compute_page_fingerprint(
        image: Any,
        page_region: tuple[int, int, int, int] | None = None,
        gesture_region: tuple[int, int, int, int] | None = None,
    ) -> str:
        """Compute a fast downsampled perceptual/spatial hash of the page background."""
        if image is None:
            return ""

        try:
            import cv2
            import numpy as np

            # If image is raw bytes, decode it
            if isinstance(image, (bytes, bytearray)):
                array = np.frombuffer(image, np.uint8)
                img = cv2.imdecode(array, cv2.IMREAD_COLOR)
            else:
                img = image

            if img is None or not hasattr(img, "shape") or len(img.shape) < 2:
                return ""

            h, w = img.shape[:2]
            # Default page_region to full frame if omitted
            px_min, py_min, px_max, py_max = page_region or (0, 0, w, h)
            px_min, py_min = max(0, px_min), max(0, py_min)
            px_max, py_max = min(w, px_max), min(h, py_max)

            if px_max <= px_min or py_max <= py_min:
                return ""

            cropped = img[py_min:py_max, px_min:px_max].copy()

            # Mask out dynamic gesture region within the cropped coordinate space
            if gesture_region is not None:
                gx_min, gy_min, gx_max, gy_max = gesture_region
                # Translate to crop coordinates
                cx1 = max(0, gx_min - px_min)
                cy1 = max(0, gy_min - py_min)
                cx2 = min(cropped.shape[1], gx_max - px_min)
                cy2 = min(cropped.shape[0], gy_max - py_min)
                if cx2 > cx1 and cy2 > cy1:
                    cropped[cy1:cy2, cx1:cx2] = 0

            # Convert to greyscale, downsample to 32x32 for lighting/jitter invariance
            gray = cv2.cvtColor(cropped, cv2.COLOR_BGR2GRAY) if len(cropped.shape) == 3 else cropped
            small = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA)

            # Difference hash (dHash) / block mean hash
            mean_val = np.mean(small)
            binary_bits = (small > mean_val).astype(np.uint8).tobytes()
            return hashlib.md5(binary_bits).hexdigest()

        except Exception as error:
            logger.debug("Perceptual fingerprint computation failed: %s", error)
            return ""

    @classmethod
    def is_compatible(
        cls,
        image: Any,
        active_page_context: PageContext | None,
        source_space: CoordinateSpace | None = None,
    ) -> bool:
        """Check whether current frame image is compatible with active PageContext."""
        if active_page_context is None or not active_page_context.ocr_words:
            return False

        if image is None:
            return False

        # Verify coordinate spaces match if provided
        if source_space is not None:
            target = active_page_context.coordinate_space
            if (
                source_space.width != target.width
                or source_space.height != target.height
                or source_space.rotation != target.rotation
                or source_space.mirror != target.mirror
            ):
                return False

        # Compute background fingerprint
        fingerprint = cls.compute_page_fingerprint(
            image,
            page_region=active_page_context.page_region,
            gesture_region=active_page_context.gesture_region,
        )

        if not fingerprint or not active_page_context.geometry_hash:
            return True

        return fingerprint == active_page_context.geometry_hash
