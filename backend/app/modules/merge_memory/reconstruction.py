"""Groq-backed semantic reconstruction for Merge Memory.

Migrated from `OCRandGESTURE/OCR_dynamicMem/taletrace_processor.py`. Three
behaviours from that file live here, because all three are semantic judgements
about text rather than anything OCR can do:

    merge_ocr_with_groq        -> `GroqReconstructor.__call__`
    check_same_page_via_groq   -> `GroqReconstructor.is_same_page`
    calculate_accurate_pointer -> `pointer_offset`

The prompts are carried over verbatim. They were tuned against real pages of a
real book, and the reconstruction quality *is* the prompt — paraphrasing it to
read better would be the exact "clean rewrite" that silently changes behaviour.

Why the Merge Engine owns this and OCR does not
-----------------------------------------------
OCR's job ends at raw text. Reconstruction repairs the *seam* between two
overlapping captures: overlap removal, broken paragraphs, headers, page numbers.
That needs both texts at once, which is precisely what Merge Memory holds and
OCR does not. Putting it in OCR would also make the OCR module know about an LLM.

The pipeline is therefore:

    OCR -> Merge Engine -> Groq reconstruction -> Merge Memory

This class satisfies the `reconstruct` callable that `MergeMemory` already
accepts, so nothing about Merge Memory's structure changes to accommodate it.

Degradation is deliberate
-------------------------
Every call falls back to the non-AI behaviour of the reference implementation
rather than raising: no key appends raw text, a failed same-page check answers
"same page". A reading session must not stop because an HTTP call did. That
matches the reference, which printed a warning and carried on.
"""

from __future__ import annotations

import logging
from difflib import SequenceMatcher
from typing import Any

from backend.app.shared.groq_keys import chat_model, fast_model, merge_engine_key

logger = logging.getLogger(__name__)

# Low temperature: this is structural text repair, not composition.
_MERGE_TEMPERATURE = 0.1

# Only the head of each text is compared. A page is identified by its opening
# just as reliably as by all of it, and this keeps the per-frame check cheap.
_PAGE_CHECK_CHARS = 400

# Migrated from `merge_ocr_with_groq`'s system prompt with one deliberate change:
# the reference named a specific book in rule 3 — `book title "Septopus: Trouble
# on the High Cs"` — and this says `book title` instead. A prompt that names one
# book only strips that book's header, and on any other book the named title is
# an instruction to look for text that is not there. The AI engine already takes
# the title from metadata for the same reason, which `test_prompt_is_book_agnostic`
# pins. Every other line is the reference's, wording included: the reconstruction
# quality *is* this text, so it is not paraphrased.
_MERGE_SYSTEM_PROMPT = """
You are the Memory Merge Engine for TaleTrace reading system.
Your SOLE responsibility is reconstructing and merging raw OCR fragments into a single, seamless, cleaned page text.

Rules:
1. Merge the 'New OCR Output' into the 'Current Merge Memory'.
2. Identify overlapping phrases, remove duplicate words, and fix broken sentences/paragraphs caused by camera movement.
3. Remove running headers (author name, book title) and standalone page numbers.
4. Smoothly repair missing OCR words using contextual language understanding.
5. DO NOT summarize, converse, or add introductory notes. Return ONLY the fully merged, properly formatted text.
"""


def pointer_offset(old_text: str, merged_text: str) -> int:
    """Where the reading pointer belongs after a same-page merge.

    Migrated unmodified from `calculate_accurate_pointer`. The longest common
    block between the old and merged text is the part already read; the pointer
    goes to where that block ends in the *merged* text (`match.b + match.size`),
    which is where genuinely new text begins.

    Reusing the offset from the old text would be wrong whenever reconstruction
    inserted a repaired word earlier in the page, because every later position
    shifts.
    """

    if not old_text:
        return 0

    matcher = SequenceMatcher(None, old_text, merged_text)
    match = matcher.find_longest_match(0, len(old_text), 0, len(merged_text))
    return match.b + match.size


class GroqReconstructor:
    """The `reconstruct` callable Merge Memory accepts, backed by Groq.

    Constructing this never opens a connection: the client is built on first use
    so the app boots, and the test suite runs, without a key.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        merge_model: str | None = None,
        page_check_model: str | None = None,
        client: Any = None,
    ) -> None:
        # `GROQ_API_KEY_2`, never the AI Engine's key. The camera loop calls this
        # several times a second and the AI Engine calls its own key once per
        # reader request; one shared credential makes a rate limit hit by the
        # former stop the latter. See `shared/groq_keys.py`.
        self._api_key = api_key if api_key is not None else merge_engine_key()
        # Resolved here rather than as a default argument: a default is evaluated
        # at import, and several entry points load the `.env` afterwards.
        # The reference's capable/fast split, kept — the same-page check runs on
        # every frame that might be a turn, so it must not wait on the big model.
        self._merge_model = merge_model or chat_model()
        self._page_check_model = page_check_model or fast_model()
        self._client = client

    @property
    def available(self) -> bool:
        """Whether reconstruction can actually reach Groq.

        Callers use this to report *why* text looks rough, rather than leaving a
        keyless session looking like a broken one.
        """

        return bool(self._client or self._api_key)

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not self._api_key:
            return None
        try:
            from groq import Groq
        except ImportError:  # pragma: no cover - exercised by absence
            logger.warning("groq package is not installed; merging raw OCR text instead")
            return None
        self._client = Groq(api_key=self._api_key)
        return self._client

    def __call__(self, current_memory: str, new_ocr_text: str) -> str:
        """Merge new OCR text into the page held so far.

        Signature is `(str, str) -> str` because that is what
        `MergeMemory(reconstruct=...)` already expects.
        """

        client = self._get_client()
        if client is None:
            # Reference behaviour with no key: append so reading still works.
            return f"{current_memory}\n{new_ocr_text}".strip()

        user_prompt = (
            "Current Merge Memory:\n"
            f"{current_memory if current_memory else '[EMPTY - PAGE START]'}\n\n"
            "New OCR Output:\n"
            f"{new_ocr_text}\n\n"
            "Updated Merged Memory:\n"
        )

        try:
            completion = client.chat.completions.create(
                messages=[
                    {"role": "system", "content": _MERGE_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                model=self._merge_model,
                temperature=_MERGE_TEMPERATURE,
            )
            merged = (completion.choices[0].message.content or "").strip()
        except Exception as error:
            logger.warning("Groq merge failed (%s); appending raw OCR text", error)
            return f"{current_memory}\n{new_ocr_text}".strip()

        # An empty completion would silently erase the page.
        return merged or f"{current_memory}\n{new_ocr_text}".strip()

    def is_same_page(self, active_memory: str, raw_ocr: str) -> bool:
        """Whether a frame is still the page already being held.

        Migrated from `check_same_page_via_groq`. This exists because geometric
        comparison cannot tell "the camera moved" from "the page turned": a
        shaky or partial capture of the same page shares few exact words with
        what is held, and treating that as a page turn would commit a
        half-finished page and reset the reading pointer mid-sentence.

        Defaults to True on every uncertain path — no text yet, no key, or a
        failed call. A false negative discards a page; a false positive merely
        merges two texts that reconstruction is already built to reconcile.
        """

        if not active_memory.strip():
            return True

        client = self._get_client()
        if client is None:
            return True

        prompt = (
            "Analyze these two text blocks from a reading camera stream:\n\n"
            "EXISTING ACTIVE PAGE MEMORY:\n"
            f'"{active_memory[:_PAGE_CHECK_CHARS]}"\n\n'
            "NEW OCR FRAME CAPTURE:\n"
            f'"{raw_ocr[:_PAGE_CHECK_CHARS]}"\n\n'
            "Are both texts reading from the SAME page or chapter? "
            "(Even if words are slightly reordered, missing, or overlapping).\n"
            "Answer with strictly ONE word: YES or NO."
        )

        try:
            response = client.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                model=self._page_check_model,
                temperature=0.0,
                max_tokens=5,
            )
            answer = (response.choices[0].message.content or "").strip().upper()
        except Exception as error:
            logger.warning("Groq same-page check failed (%s); assuming same page", error)
            return True

        return "YES" in answer
