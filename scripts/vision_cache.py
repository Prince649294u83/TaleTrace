"""One Google Vision call per image, ever — shared by every dev script.

Vision is the only stage of the pipeline that costs money on every run and gives
the same answer every time, which makes it the one stage worth caching outright.
The response is stored under `.taletrace_cache/vision/` and replayed through the
production parser from then on, so re-running a harness while debugging the Audio
Engine or the reading pointer costs nothing.

The store itself now lives in `backend.app.modules.ocr.vision_cache`, because
Simulation Mode needs the same entries and `backend/` must not import from
`scripts/`. What is left here is the *fetching* — the API call, the key check, the
progress note — which is a harness concern and not the application's. The key
derivation and the file layout are imported rather than repeated, so the two
cannot drift: a second `sha256(...)[:32]` in this file would be a cache that
happened to agree until someone changed one of them.

Shared rather than duplicated because two scripts need the same entries. If
`validate_pipeline` and `equivalence_probe` each hashed the bytes their own way
they would keep two caches of the same photograph and each would pay for its own
first call — and the differential probe would then be comparing the reference
against a *different* Vision response than the one the acceptance run validated,
which is the one thing it must not do.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.modules.ocr.pipeline import OcrPipeline  # noqa: E402
from backend.app.modules.ocr.replay import ReplayAdapter  # noqa: E402
from backend.app.modules.ocr.vision_cache import (  # noqa: E402
    CACHE_DIR,
    cache_path,
    read_cached,
    write_cached,
)

__all__ = ["CACHE_DIR", "cache_path", "fetch_once", "preprocess"]


def preprocess(raw: bytes) -> bytes:
    """The bytes a live session would submit, through the pipeline's own `_enhance`.

    The production path and not a copy of it: CLAHE and sharpening change what
    Vision reads, so preprocessing the image any other way here would cache a
    response describing an image the pipeline never sends.
    """

    return OcrPipeline(provider=ReplayAdapter())._enhance(raw)


def fetch_once(
    image_path: Path,
    *,
    fresh: bool = False,
    note: Callable[[str], None] = print,
) -> tuple[dict[str, Any], bytes, bool]:
    """Get this image's Vision response, calling the API at most once ever.

    Returns `(response, preprocessed_bytes, came_from_cache)`. The bytes are
    returned as well as the response because callers that want to compare
    renderings have to feed both sides the same input, and re-deriving them would
    let the two drift.
    """

    raw = image_path.read_bytes()
    prepared = preprocess(raw)

    if not fresh:
        cached = read_cached(prepared)
        if cached is not None:
            return cached, prepared, True

    key = os.environ.get("GOOGLE_VISION_API_KEY", "").strip()
    if not key:
        raise SystemExit(
            "GOOGLE_VISION_API_KEY is not set and no cached OCR exists for this image.\n"
            "OCR is the one hard dependency: without it there is no text to validate."
        )

    import base64

    import requests

    note("calling Google Vision (once — the response is cached from here on)")
    response = requests.post(
        f"https://vision.googleapis.com/v1/images:annotate?key={key}",
        json={
            "requests": [
                {
                    "image": {"content": base64.b64encode(prepared).decode("utf-8")},
                    "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
                }
            ]
        },
        timeout=45,
    )
    if response.status_code != 200:
        raise SystemExit(
            f"Google Vision request failed (HTTP {response.status_code}): {response.text[:300]}"
        )

    payload = response.json()
    write_cached(prepared, payload)
    return payload, prepared, False
