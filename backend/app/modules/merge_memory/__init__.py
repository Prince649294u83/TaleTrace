"""Merge Memory: the single source of truth for what the book says.

The contract lives in `shared/merge_memory.py` as a Protocol; `MergeMemory` here
is its one implementation. See `engine.py` for why the implementation is not in
the shared layer.

`reconstruction.py` holds the Groq-backed semantic repair that turns overlapping
OCR captures into clean page text, plus the same-page check and the reading
pointer offset. The Merge Engine owns those because they are judgements about
text, which is what it holds and OCR does not.
"""

from backend.app.modules.merge_memory.engine import MergeMemory, Page
from backend.app.modules.merge_memory.reconstruction import (
    GroqReconstructor,
    pointer_offset,
)

__all__ = ["GroqReconstructor", "MergeMemory", "Page", "pointer_offset"]
