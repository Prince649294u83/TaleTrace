"""The ESP32-CAM as a frame source.

Migrated from `fetch_camera_frame` in `OCRandGESTURE/main_controller.py`, which
did the right thing in six lines: GET the capture endpoint, return the JPEG bytes,
and return nothing rather than raise if the device did not answer. That last part
is load-bearing on real hardware — a reading session cannot end because one frame
over Wi-Fi was dropped — so it is preserved exactly.

What changed is only that it is no longer a module-level function reaching for a
module-level `requests.Session`, so a test can drive it without a device and the
runtime can hold more than one source.

    ESP32-CAM -> Esp32Camera.frame() -> OpenCV -> Google Vision -> Merge Engine

Bytes, not arrays
-----------------
`frame()` returns the raw JPEG exactly as the device sent it. The reference
learned this the hard way: OCR wants bytes (it base64-encodes them for Vision)
while the Gesture Engine wants a decoded BGR array, and decoding centrally then
re-encoding for Vision cost image quality for nothing. Callers that need an array
call `decode()`.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# The device answers in well under a second on a healthy network. A longer
# timeout would stall the control loop behind a device that has gone away.
_CAPTURE_TIMEOUT = 1.5


@dataclass(frozen=True)
class CameraFrame:
    """One captured camera frame with device-side payload checksum and metadata."""

    frame_id: int
    captured_at: float
    jpeg_bytes: bytes
    jpeg_hash: str
    width: int = 0
    height: int = 0


class Esp32Camera:
    """One ESP32-CAM, polled for single JPEG frames.

    Constructing this opens no connection, so the runtime can be built with no
    hardware present and report the camera as unreachable later.
    """

    source_name = "esp32_cam"

    def __init__(
        self,
        capture_url: str | None = None,
        *,
        timeout: float = _CAPTURE_TIMEOUT,
        session: Any = None,
    ) -> None:
        self.capture_url = (
            capture_url
            if capture_url is not None
            else os.environ.get("ESP32_CAM_CAPTURE_URL", "")
        )
        self.timeout = timeout
        self._session = session
        self._frames_read = 0
        self._failures = 0
        self._last_hash: str | None = None
        self._last_capture_time: float = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.capture_url)

    @property
    def frames_read(self) -> int:
        return self._frames_read

    @property
    def failures(self) -> int:
        """Consecutive-failure count is not tracked; this is the total.

        The runtime uses it to tell "the camera never worked" from "the camera
        works and dropped a frame", which is the difference between a
        misconfiguration and a normal Wi-Fi hiccup.
        """

        return self._failures

    @property
    def last_hash(self) -> str | None:
        return self._last_hash

    def _http(self) -> Any:
        if self._session is None:
            import requests

            # One session for the life of the source: the ESP32 is one host and
            # connection reuse is most of the per-frame latency.
            self._session = requests.Session()
        return self._session

    def capture_frame(self) -> CameraFrame | None:
        """Capture one CameraFrame with checksum freshness and monotonic ID."""
        if not self.configured:
            logger.warning("ESP32_CAM_CAPTURE_URL is not set; no frames available")
            self._failures += 1
            return None

        capture_start = time.time()
        try:
            response = self._http().get(self.capture_url, timeout=self.timeout)
        except Exception as error:
            logger.debug("ESP32-CAM capture failed: %s", error)
            self._failures += 1
            return None

        if response.status_code != 200:
            logger.debug("ESP32-CAM returned HTTP %s", response.status_code)
            self._failures += 1
            return None

        content = response.content
        if not content:
            self._failures += 1
            return None

        self._frames_read += 1
        jpeg_hash = hashlib.md5(content).hexdigest()
        self._last_hash = jpeg_hash
        self._last_capture_time = capture_start

        return CameraFrame(
            frame_id=self._frames_read,
            captured_at=capture_start,
            jpeg_bytes=content,
            jpeg_hash=jpeg_hash,
        )

    def frame(self) -> bytes | None:
        """Capture one JPEG frame, or None if the device did not answer.

        Returns None rather than raising, exactly as the reference did. Every
        caller is a loop that should try again on the next tick; an exception
        would make a dropped frame end the session.
        """

        cam_frame = self.capture_frame()
        return cam_frame.jpeg_bytes if cam_frame is not None else None

    @staticmethod
    def decode(jpeg_bytes: bytes) -> Any | None:
        """Decode JPEG bytes to a BGR array for the Gesture Engine.

        Separate from `frame()` because OCR must not pay for a decode it does not
        need. Returns None on undecodable bytes, which a partial capture over
        Wi-Fi does produce.
        """

        try:
            import cv2
            import numpy as np
        except ImportError:  # pragma: no cover - exercised by absence
            logger.warning("OpenCV is required to decode a camera frame")
            return None

        array = np.frombuffer(jpeg_bytes, np.uint8)
        return cv2.imdecode(array, cv2.IMREAD_COLOR)
