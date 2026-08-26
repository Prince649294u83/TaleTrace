"""The seam between the session and the AI Engine.

The Reading Engine knows where the reader is; the AI Engine knows how to ask a
model about a word. Neither should have to know the other's shape, so this
translates between them:

    ReadingEngine state ──> ReadingContext ──> Groq ──> AiCapabilityResponse
                       <── plain dicts   <───────────┘

Three problems live here rather than in either module.

*The AI Engine is synchronous.* Every engine in `ai_engine.engines` makes a
blocking HTTP call. Awaiting one from the Reading Engine would stall the audio
playback loop for the duration of a network round trip — several seconds while a
sentence sits half-spoken. Every call goes through `asyncio.to_thread`.

*Context has to be assembled.* `ExplanationEngine` wants the containing
paragraph, the one before it, the page number, and the words already explained
this session. Those are four different owners' data. Gathering it at the call
site would put book metadata handling inside the pointer-writing class.

*The model can fail, and a session must not.* A missing API key, a timeout, a
malformed payload — all of them arrive here as `status == "error"`, and all of
them mean the same thing to the reader: no explanation appeared. Narration keeps
playing and the pointer keeps moving, because a lookup that failed is a lookup
that did not happen, not a session that ended.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from backend.app.modules.ai_engine.engines import (
    ExplanationEngine,
    NovelMode,
    SummaryGenerator,
)
from backend.app.modules.learning_engine.engines import LearningEngine
from backend.app.modules.learning_engine.models import LearningEngineRequest
from backend.app.modules.database.review import validate_learning_material
from backend.app.modules.ai_engine.models import (
    AiExplainRequest,
    AiInput,
    AiSessionSummaryRequest,
    BookMetadata,
    LearnedWord,
    LookupRecord,
    ReadingContext,
    ReadingMode,
)
from backend.app.shared.groq_keys import ai_engine_key

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AiOutcome:
    """What came back, flattened to what a caller actually branches on.

    `ok` is separate from `data` because an error payload is still a dict and
    still truthy. Callers that treat "got a response" as "got an explanation"
    read the model's error message out to the reader.
    """

    capability: str
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    @property
    def oled_text(self) -> str:
        """The short form, for the device's small display."""

        return str(self.data.get("oled_text", ""))

    @property
    def explanation(self) -> str:
        return str(self.data.get("full_explanation", ""))


@dataclass
class AiBridge:
    """Runs AI capabilities for one session, off the event loop.

    `book` is held rather than passed per call because it does not change during
    a session, and threading it through every gesture would mean the gesture
    handler knew the book's genre.

    Engines are injected so a scenario can drive this with a stub that raises,
    times out, or returns nonsense — the three failures worth testing and the
    three that are awkward to provoke against a live model on purpose.
    """

    book: BookMetadata = field(default_factory=BookMetadata)
    mode: ReadingMode = ReadingMode.STANDARD
    # How long to wait for any one capability before giving up on it. The Groq
    # SDK's own timeout is generous and `_safe_json_completion` retries twice on
    # top of it, so the worst case without this is three long hangs in series
    # while a reader stares at a paused page. Twenty seconds is past the point
    # where an answer is still useful as a response to a gesture.
    timeout: float = 20.0
    explanation_engine: Any = field(default_factory=ExplanationEngine)
    summary_generator: Any = field(default_factory=SummaryGenerator)
    novel_mode: Any = field(default_factory=NovelMode)
    learning_engine: Any = field(default_factory=LearningEngine)

    # Every word explained so far, in order. Fed back into later explanations so
    # the model can say "like 'gale', which you looked up on page 3" — and used
    # as the session's lookup history when the review is built.
    _learned: list[LearnedWord] = field(default_factory=list, init=False)
    _history: list[LookupRecord] = field(default_factory=list, init=False)

    @property
    def learned(self) -> list[LearnedWord]:
        return list(self._learned)

    @property
    def history(self) -> list[LookupRecord]:
        return list(self._history)

    # -------------------------------------------------------------- capabilities

    async def explain(
        self,
        *,
        word: str,
        paragraph: str,
        previous_paragraph: str = "",
        page_number: int | None = None,
    ) -> AiOutcome:
        """Explain `word` as it is used in `paragraph`. Meaning Mode's payload.

        The paragraph goes with the word because the same word is not the same
        question twice: "bank" in a chapter about rivers and "bank" in one about
        money need different answers, and only the surrounding text says which.
        """

        if not word.strip():
            return AiOutcome(capability="explanation_engine", ok=False, error="No word selected")

        request = AiExplainRequest(
            text=paragraph,
            context=ReadingContext(
                book=self.book,
                page_number=page_number,
                previous_paragraph=previous_paragraph or None,
                current_paragraph=paragraph or None,
                selected_word=word,
                mode=self.mode,
                previously_explained=list(self._learned),
            ),
        )

        outcome = await self._run("explanation_engine", self.explanation_engine.explain, request)

        # A call that succeeded but explained nothing is not an explanation. The
        # engine defaults both text fields to "" when the model omits them, so
        # `status == "ok"` alone does not mean the reader was told anything —
        # recording it would put a word in the review with a blank flashcard.
        if outcome.ok and not (outcome.oled_text.strip() or outcome.explanation.strip()):
            logger.warning("[ai:explanation_engine] empty payload for %r", word)
            return AiOutcome(
                capability=outcome.capability,
                ok=False,
                data=outcome.data,
                error="AI returned no explanation",
            )

        # Recorded only on success. A failed lookup in the history would become a
        # flashcard for a word the reader was never told the meaning of.
        if outcome.ok:
            self._learned.append(
                LearnedWord(word=word, definition=outcome.explanation or None)
            )
            self._history.append(
                LookupRecord(
                    word=word,
                    context=paragraph or None,
                    mode_used=self.mode.value,
                    page_number=page_number,
                )
            )

        return outcome

    async def review(self, *, pages_read: int | None = None) -> AiOutcome:
        """Flashcards, quiz, words learned, and the session summary — one call.

        They share a call because they share an input: the same lookup history
        produces all four, and asking four times would pay four round trips to
        re-read the same list. `SummaryGenerator` fails fast on an empty history,
        which is correct — a session with no lookups has nothing to review, and
        that is not an error worth surfacing to the reader.
        """

        if not self._history:
            return AiOutcome(
                capability="summary_generator",
                ok=False,
                error="No lookups recorded this session",
            )

        # 1. AI Engine (GROQ_API_KEY_1) for summary and words learned
        summary_request = AiSessionSummaryRequest(
            context=ReadingContext(
                book=self.book,
                page_number=pages_read,
                mode=self.mode,
                previously_explained=list(self._learned),
            ),
            session_history=list(self._history),
        )

        outcome = await self._run("summary_generator", self.summary_generator.summarize, summary_request)
        
        # 2. Learning Engine (GROQ_API_KEY_3) for quiz and flashcards
        # This gracefully defaults to empty lists on failure, avoiding session abortion.
        learning_request = LearningEngineRequest(
            context=summary_request.context,
            session_history=[{"word": h.word, "context": h.context} for h in self._history if h.context],
            session_summary=str(outcome.data.get("session_summary", "")),
            words_learned=outcome.data.get("words_learned", []),
        )

        try:
            learning_response = await asyncio.wait_for(
                asyncio.to_thread(self.learning_engine.generate, learning_request), 
                timeout=self.timeout
            )
            raw_learning = {
                "quiz": learning_response.quiz,
                "flashcards": learning_response.flashcards,
                "words_learned": outcome.data.get("words_learned", [])
            }
        except Exception as error:
            logger.warning("[ai:learning_engine] call raised: %s", error)
            raw_learning = {"quiz": [], "flashcards": [], "words_learned": outcome.data.get("words_learned", [])}

        # 3. Validation layer
        validated = validate_learning_material(raw_learning)
        
        # 4. Merge into final expected payload
        outcome.data["quiz"] = validated["quiz"]
        outcome.data["flashcards"] = validated["flashcards"]
        
        return outcome

    async def scene_mood(self, *, paragraph: str, page_number: int | None = None) -> AiOutcome:
        """Classify the passage's mood, for Novel Mode's ambient audio.

        Separate from narration on purpose: this picks the background bed, and a
        failure here means the page is read without ambience rather than not read.
        """

        request = AiInput(
            text=paragraph,
            context=ReadingContext(
                book=self.book,
                page_number=page_number,
                current_paragraph=paragraph or None,
                mode=self.mode,
            ),
        )

        return await self._run("novel_mode", self.novel_mode.create, request)

    # ------------------------------------------------------------------ plumbing

    async def _run(self, capability: str, fn: Any, request: Any) -> AiOutcome:
        """Call one engine in a worker thread, bounded, and normalise every failure.

        `engines._safe_json_completion` already retries twice and converts a
        raised exception into an `{"error": ...}` payload, so the usual failures
        arrive as a response with `status == "error"`. The try/except is for the
        ones that escape it — an engine that raises before reaching the retry
        wrapper, or a stub in a scenario doing exactly that on purpose.

        The timeout stops waiting; it cannot stop the thread. A blocking socket
        read is not interruptible from outside, so the worker runs to completion
        and its result is discarded. That is the correct trade here: the thread
        is idle-waiting on a socket, and the alternative is a reader whose page
        stays paused for as long as the upstream takes to notice it is dead.
        """

        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(fn, request), timeout=self.timeout
            )
        except asyncio.TimeoutError:
            logger.warning("[ai:%s] timed out after %.0fs", capability, self.timeout)
            return AiOutcome(
                capability=capability,
                ok=False,
                error=f"AI call timed out after {self.timeout:.0f}s",
            )
        except Exception as error:  # noqa: BLE001 — a failed lookup must not end a session
            logger.warning("[ai:%s] call raised: %s", capability, error)
            return AiOutcome(capability=capability, ok=False, error=str(error))

        data = dict(getattr(response, "data", {}) or {})
        ok = getattr(response, "status", "error") == "ok"

        if not ok:
            error = str(data.get("error") or getattr(response, "message", "") or "AI call failed")
            logger.warning("[ai:%s] %s", capability, error)
            return AiOutcome(capability=capability, ok=False, data=data, error=error)

        return AiOutcome(capability=capability, ok=True, data=data)


def bridge_for_session() -> AiBridge | None:
    """The session's AI Engine, or `None` when `GROQ_API_KEY_1` is absent.

    The one place a running session gets a bridge, so the rig and the simulator
    cannot disagree about how the AI Engine is attached — they disagreed by
    omission for a while, both leaving `ai` unset, which made `meaning_mode_on`
    skip `explain()` and `finish_session` skip `review()`. The consequence was a
    reader whose Meaning Mode press did nothing and a website whose AI Summary
    was empty on every session, with no error anywhere: both call sites guard on
    `self.ai is not None`, so an absent engine looks exactly like a session with
    nothing to say.

    `None` rather than a keyless bridge, because a bridge with no key spends the
    reader's pause failing three calls in series before saying so. Absence is
    reported once, by the runner, before the session starts.

    No `BookMetadata`: a live session does not know what book is in front of the
    camera. The demo passes one because it chose the book.
    """

    return AiBridge() if ai_engine_key() else None
