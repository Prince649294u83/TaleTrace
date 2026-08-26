"""Which Groq key and model each subsystem uses, and why there are two of each.

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

This module resolves credentials and model names only. It deliberately does not
build, cache or hand out a client: each subsystem constructs its own, so neither
holds a reference to the other's and no accidental sharing is possible.

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

# Both subsystems' model names, here rather than one per module. They were one per
# module, and when Groq retired `llama-3.3-70b-versatile` that meant three stale
# copies: every explanation 404'd three times and returned an error payload, every
# merge fell back to raw OCR, and `--check` still reported both engines OK because
# it only ever looked at the keys. A model name is a vendor's decision with a shelf
# life. One home, so the next retirement is one edit and one preflight row.
CHAT_MODEL_VARIABLE = "GROQ_MODEL"
FAST_MODEL_VARIABLE = "GROQ_FAST_MODEL"

_CHAT_MODEL = "openai/gpt-oss-120b"
_FAST_MODEL = "openai/gpt-oss-20b"


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


def chat_model() -> str:
    """The model for explanations and for text reconstruction.

    A function, not a constant, because several entry points call
    `load_environment()` *after* importing the engines — a name captured at import
    time is the one from before the `.env` was read.
    """

    return (os.environ.get(CHAT_MODEL_VARIABLE) or "").strip() or _CHAT_MODEL


def fast_model() -> str:
    """The model for the same-page check, which runs on every frame that may be a turn."""

    return (os.environ.get(FAST_MODEL_VARIABLE) or "").strip() or _FAST_MODEL


def describe() -> list[tuple[str, str, bool]]:
    """`(subsystem, variable, present)` for each key, for preflight to print.

    Presence only. The value never leaves this module — a key printed once into a
    terminal scrollback or a log file is a key that has to be rotated.
    """

    return [
        ("AI Engine", AI_ENGINE_VARIABLE, bool(ai_engine_key())),
        ("Merge Engine", MERGE_ENGINE_VARIABLE, bool(merge_engine_key())),
    ]
