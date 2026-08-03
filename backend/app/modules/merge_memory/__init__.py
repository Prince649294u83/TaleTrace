"""Merge Memory: the single source of truth for what the book says.

The contract lives in `shared/merge_memory.py` as a Protocol; `MergeMemory` here
is its one implementation. See `engine.py` for why the implementation is not in
the shared layer.
"""

from backend.app.modules.merge_memory.engine import MergeMemory, Page

__all__ = ["MergeMemory", "Page"]
