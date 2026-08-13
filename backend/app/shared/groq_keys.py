"""Which Groq key each subsystem uses, and why there are two of them.

Two subsystems in TaleTrace call Groq, and they call it for unrelated reasons:

    GROQ_API_KEY_1  AI Engine — Meaning Mode, contextual explanations, question
                    answering, session review, and whatever tutoring comes later.
                    One call per reader request, on a big model, latency measured
                    against a person waiting.
    GROQ_API_KEY_2  Merge Engine — dynamic memory reconstruction, OCR cleanup,
                    text reconstruction, same-page semantic comparison. Everything
                    migrated out of `taletrace_processor.py`. Several calls per
                    second while the camera is running, and nobody is waiting on
                    any single one.

They are split because they fail differently and should not take each other down.
A rate limit hit by the camera loop must not stop a reader from asking what a word
means; a quota exhausted by explanations must not stop the page from being read.
Sharing one key makes those two failures the same failure, and the busier
subsystem always causes it.

This module resolves credentials only. It deliberately does not build, cache or
hand out a client: each subsystem constructs its own, so neither holds a reference
to the other's and no accidental sharing is possible.

The fallback to `GROQ_API_KEY` is for the reference tree and for any machine still
on a single-key `.env`. It is a place to *find* a credential, not a shared client —
a deployment that sets only the old variable gets the old single-key behaviour,
and one that sets the new pair gets full separation.
"""

from __future__ import annotations

import os

# The legacy single key. Read only when the subsystem's own variable is unset.
_LEGACY = "GROQ_API_KEY"

AI_ENGINE_VARIABLE = "GROQ_API_KEY_1"
MERGE_ENGINE_VARIABLE = "GROQ_API_KEY_2"


def _resolve(variable: str) -> str:
    """The subsystem's own key, or the legacy shared one, or empty."""

    own = (os.environ.get(variable) or "").strip()
    if own:
        return own
    return (os.environ.get(_LEGACY) or "").strip()


def ai_engine_key() -> str:
    """The key for explanations, Meaning Mode and session review. Never the merge key."""

    return _resolve(AI_ENGINE_VARIABLE)


def merge_engine_key() -> str:
    """The key for reconstruction, OCR cleanup and the same-page check. Never the AI key."""

    return _resolve(MERGE_ENGINE_VARIABLE)


def describe() -> list[tuple[str, str, bool]]:
    """`(subsystem, variable, present)` for each key, for preflight to print.

    Presence only. The value never leaves this module — a key printed once into a
    terminal scrollback or a log file is a key that has to be rotated.
    """

    return [
        ("AI Engine", AI_ENGINE_VARIABLE, bool(ai_engine_key())),
        ("Merge Engine", MERGE_ENGINE_VARIABLE, bool(merge_engine_key())),
    ]
