"""Recorded Vision responses on disk, and the offline provider that replays them.

Google Vision is the only production OCR engine and this does not add a second
one. What it adds is a *store*: the response Vision gave for a set of bytes, kept
so the same bytes never have to be paid for twice. `CachedVisionProvider` is the
replay path with a key derivation in front of it — the JPEG bytes are hashed, the
recorded response is looked up, and the production parser turns it into words.
Deliberately absent from `get_ocr_engine()`'s registry, so no production code can
reach it by name.

    live      bytes ──► Google Vision ──► parse_response ──► words
    cached    bytes ──► hash ──► recorded response ──► parse_response ──► words
                                        (the same parser, the same words)

Why this is in the module and not in the harness
------------------------------------------------
It began in `scripts/vision_cache.py`, which was the right place while only dev
scripts used it. Simulation Mode needs it too — a five-hundred-image stress run
cannot cost five hundred Vision calls to report that the audio queue leaked — and
`backend/` importing from `scripts/` would invert the dependency: the application
would need the harness present to start. So the store lives here and the harness
delegates to it. One cache, one hash, one place a change to `_enhance` invalidates.

Keyed on the preprocessed bytes
-------------------------------
Not the file. CLAHE and sharpening change what Vision reads, so the preprocessed
bytes are the ones Vision actually saw. Two files that enhance to the same image
share an entry, and any change to `_enhance` invalidates every entry — which is
correct, because the old response describes an image the pipeline no longer sends.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from backend.app.modules.ocr.models import RecognizedWord
from backend.app.modules.ocr.providers import GoogleVisionProvider, OcrProviderError

logger = logging.getLogger(__name__)

# Repo root: this file is backend/app/modules/ocr/vision_cache.py, so up five.
_ROOT = Path(__file__).resolve().parents[4]
CACHE_DIR = _ROOT / ".taletrace_cache" / "vision"


def cache_key(prepared: bytes) -> str:
    """The cache key for a set of preprocessed bytes."""

    return hashlib.sha256(prepared).hexdigest()[:32]


def cache_path(prepared: bytes, *, directory: Path | None = None) -> Path:
    """Where this image's recorded Vision response lives."""

    return (directory or CACHE_DIR) / f"{cache_key(prepared)}.json"


def read_cached(prepared: bytes, *, directory: Path | None = None) -> dict[str, Any] | None:
    """The recorded response for these bytes, or None if there is none."""

    path = cache_path(prepared, directory=directory)
    if not path.is_file():
        return None
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        # A corrupt entry is a cache miss, not a crash: the response can always be
        # fetched again, and a half-written file from an interrupted run should not
        # be able to stop a session from starting.
        logger.warning("ignoring unreadable Vision cache entry %s: %s", path.name, error)
        return None


def write_cached(
    prepared: bytes, response: dict[str, Any], *, directory: Path | None = None
) -> Path:
    """Record a Vision response for these bytes. Returns where it was written."""

    path = cache_path(prepared, directory=directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(response, handle, ensure_ascii=False)
    return path


class CachedVisionProvider:
    """Frame bytes in, words out, from recorded responses only. Never calls out.

    The OCR provider Simulation Mode uses when `--offline` is set. It takes the
    same input the live provider takes — the preprocessed bytes the pipeline hands
    every provider — so the pipeline, the parser and every word index downstream
    are identical to a live run. The only difference is where the response came
    from.

    A miss raises rather than returning nothing. An empty word list would look
    exactly like a page with no text on it, and a stress run would then report a
    hundred pages of successful OCR that never happened.
    """

    provider_name = "vision-cache"

    def __init__(self, *, directory: Path | None = None, strict: bool = True) -> None:
        self.directory = directory or CACHE_DIR
        self.strict = strict
        self.hits = 0
        self.misses = 0

    def accepts(self, source: Any) -> bool:
        return isinstance(source, (bytes, bytearray))

    def extract(self, source: Any) -> list[RecognizedWord]:
        if not isinstance(source, (bytes, bytearray)):
            raise OcrProviderError(
                f"{self.provider_name} needs image bytes, got {type(source).__name__}"
            )

        prepared = bytes(source)
        response = read_cached(prepared, directory=self.directory)
        if response is None:
            self.misses += 1
            if self.strict:
                raise OcrProviderError(
                    f"no cached Vision response for these bytes "
                    f"({cache_key(prepared)}). Run the image through a live harness "
                    f"once to record one, or drop --offline."
                )
            return []

        self.hits += 1
        # The production parser, not a copy of it: the word indices, line indices
        # and paragraph indices every downstream module reads are the ones Vision's
        # own hierarchy produces, and a second parser here would let them drift.
        return GoogleVisionProvider.parse_response(response)
