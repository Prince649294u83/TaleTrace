"""Real AI Engine implementations (Groq-backed) matching the Protocol interfaces
defined in interfaces.py. These replace the static placeholders in
placeholders.py once wired into router.py.

Prompts are built by PromptBuilder from the reading context supplied by the
Reading Engine, so no engine is tied to a specific book.
"""

import json
import logging

from dotenv import load_dotenv
from groq import Groq

from backend.app.modules.ai_engine.models import (
    AiCapabilityResponse,
    AiExplainRequest,
    AiInput,
    AiSessionSummaryRequest,
    DifficultyLevel,
    ImageType,
)
from backend.app.modules.ai_engine.prompts import PromptBuilder
from backend.app.shared.groq_keys import ai_engine_key, chat_model

load_dotenv()

logger = logging.getLogger(__name__)

_client: Groq | None = None
_prompts = PromptBuilder()


def _get_client() -> Groq:
    """Create the Groq client on first use so the app can boot without a key.

    `GROQ_API_KEY_1`, never the Merge Engine's key. This client belongs to the AI
    Engine alone — the Merge Engine builds its own in
    `merge_memory/reconstruction.py`, and neither holds a reference to the other's.
    A reader asking what a word means must not be rate-limited by the camera loop.
    """

    global _client
    if _client is None:
        _client = Groq(api_key=ai_engine_key())
    return _client


def reset_client() -> None:
    """Drop the cached client so the next call re-reads the key.

    Exists for the harnesses: `.env` is loaded after import in several entry
    points, and a client built from an empty environment would otherwise be
    cached for the life of the process.
    """

    global _client
    _client = None


def _safe_json_completion(system_prompt: str, user_content: str, max_retries: int = 2) -> dict:
    """Shared retry wrapper: Groq occasionally emits malformed JSON on complex schemas.

    Uses JSON mode. Moving to a `json_schema` response format would give
    stronger guarantees once every engine's schema is pinned down.
    """

    last_error = None
    for _ in range(max_retries + 1):
        try:
            response = _get_client().chat.completions.create(
                model=chat_model(),
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                response_format={"type": "json_object"},
            )
            return json.loads(response.choices[0].message.content)
        except Exception as e:  # noqa: BLE001 — surfaced to the caller as an error payload
            last_error = e
            logger.warning("Groq JSON completion attempt failed: %s", e)
            continue
    return {"error": f"Failed after {max_retries + 1} attempts: {str(last_error)}"}


def _respond(capability: str, result: dict) -> AiCapabilityResponse:
    """Wrap a parsed payload in the shared response envelope."""

    failed = "error" in result
    return AiCapabilityResponse(
        status="error" if failed else "ok",
        capability=capability,
        message=json.dumps(result),
        data=result,
    )


def _clamp_intensity(value: object, default: float = 0.5) -> float:
    """Coerce a model-supplied intensity into the 0.0–1.0 range."""

    try:
        return round(min(1.0, max(0.0, float(value))), 2)
    except (TypeError, ValueError):
        return default


class ExplanationEngine:
    """Implements ExplanationEngineInterface.

    Reads the full reading context (book, page, previous and current paragraph,
    selected word, mode, session memory) and returns an OLED-sized phrase, a
    full explanation, and the word's difficulty level.
    """

    capability = "explanation_engine"

    def explain(self, request: AiExplainRequest) -> AiCapabilityResponse:
        context = request.resolved_context()

        result = _safe_json_completion(
            _prompts.explanation_prompt(context),
            _prompts.user_content(context),
        )

        if "error" not in result:
            result.setdefault("oled_text", "")
            result.setdefault("full_explanation", "")
            result["difficulty_level"] = self._normalize_difficulty(
                result.get("difficulty_level")
            )

        return _respond(self.capability, result)

    @staticmethod
    def _normalize_difficulty(value: object) -> str:
        try:
            return DifficultyLevel(str(value).strip().lower()).value
        except ValueError:
            return DifficultyLevel.INTERMEDIATE.value


class ImageDecision:
    """Implements ImageDecisionInterface. Decides whether a supporting image
    would help explain the word, what to search for, and what style of image to
    retrieve. Does NOT fetch the image itself — that's a separate concern."""

    capability = "image_decision"

    def decide(self, request: AiInput) -> AiCapabilityResponse:
        context = request.resolved_context()

        result = _safe_json_completion(
            _prompts.image_decision_prompt(context),
            _prompts.user_content(context),
        )

        if "error" not in result:
            show_image = bool(result.get("show_image"))
            result["show_image"] = show_image
            result["image_query"] = str(result.get("image_query") or "") if show_image else ""
            result["image_type"] = self._normalize_image_type(
                result.get("image_type"), show_image
            )
            result.setdefault("reason", "")

        return _respond(self.capability, result)

    @staticmethod
    def _normalize_image_type(value: object, show_image: bool) -> str:
        if not show_image:
            return ImageType.NONE.value
        try:
            image_type = ImageType(str(value).strip().lower())
        except ValueError:
            return ImageType.ILLUSTRATION.value
        return (
            ImageType.ILLUSTRATION.value
            if image_type is ImageType.NONE
            else image_type.value
        )


class NovelMode:
    """Implements NovelModeInterface.

    Classifies a passage into a genre-neutral scene mood and reports the
    dominant emotion plus its intensity, so ambient audio can crossfade rather
    than switch abruptly.
    """

    capability = "novel_mode"

    AUDIO_TAGS = {
        "peaceful": "sfx_soft_ambient.mp3",
        "wonder": "sfx_calm_bubbles.mp3",
        "musical": "sfx_quirky_orchestra.mp3",
        "suspense": "sfx_sneaky_caper.mp3",
        "action": "sfx_upbeat_adventure.mp3",
        "comedy": "sfx_bouncing_comedy.mp3",
        "sorrow": "sfx_gentle_melancholy.mp3",
        "neutral_narration": "sfx_soft_ambient.mp3",
    }

    SCENE_MOODS = {
        "peaceful": "calm settings, gentle dialogue, rest, everyday life",
        "wonder": "discovery, awe, magic, beauty, curiosity",
        "musical": "music, rehearsal, performance, song, instruments",
        "suspense": "threat, scheming, secrecy, fear, rising danger",
        "action": "chases, rescues, fights, urgency, high stakes",
        "comedy": "jokes, mishaps, slapstick, absurdity, mistaken identity",
        "sorrow": "loss, grief, loneliness, regret",
        "neutral_narration": "connective narration or anything that does not clearly fit",
    }

    def create(self, request: AiInput) -> AiCapabilityResponse:
        context = request.resolved_context()
        mood_lines = [f'"{mood}" ({hint})' for mood, hint in self.SCENE_MOODS.items()]

        result = _safe_json_completion(
            _prompts.novel_mode_prompt(context, mood_lines),
            _prompts.user_content(context),
        )

        if "error" not in result:
            mood = str(result.get("scene_mood", "")).strip().lower()
            if mood not in self.AUDIO_TAGS:
                mood = "neutral_narration"
            result["scene_mood"] = mood
            result["audio_tag"] = self.AUDIO_TAGS[mood]
            result["emotion"] = str(result.get("emotion") or "neutral").strip().lower()
            result["intensity"] = _clamp_intensity(result.get("intensity"))
            result.setdefault("companion_commentary", "")

        return _respond(self.capability, result)


class SummaryGenerator:
    """Implements SummaryGeneratorInterface (covers flashcards + quiz + words
    learned + summary together for now). Expects an AiSessionSummaryRequest
    with a real session_history list field."""

    capability = "summary_generator"

    def summarize(self, request: AiSessionSummaryRequest) -> AiCapabilityResponse:
        session_history = request.session_history

        if not session_history:
            return _respond(
                self.capability, {"error": "No lookups recorded this session"}
            )

        context = request.resolved_context()
        result = _safe_json_completion(
            _prompts.summary_prompt(context),
            f"session_history:\n{self._render_history(session_history)}",
        )

        if "error" not in result:
            result.setdefault("flashcards", [])
            result.setdefault("quiz", [])
            result.setdefault("session_summary", "")
            if not result.get("words_learned"):
                result["words_learned"] = self._fallback_words_learned(session_history)

        return _respond(self.capability, result)

    @staticmethod
    def _render_history(session_history: list) -> str:
        lines = []
        for item in session_history:
            parts = [f"- Word: '{item.word}'"]
            if item.context:
                parts.append(f'Context: "{item.context}"')
            if item.mode_used:
                parts.append(f"Mode used: {item.mode_used}")
            if item.page_number is not None:
                parts.append(f"Page: {item.page_number}")
            lines.append(" | ".join(parts))
        return "\n".join(lines)

    @staticmethod
    def _fallback_words_learned(session_history: list) -> list[dict[str, str]]:
        """Guarantee the analytics-facing word list even if the model omits it."""

        seen: dict[str, None] = {}
        for item in session_history:
            seen.setdefault(item.word, None)
        return [{"word": word, "takeaway": ""} for word in seen]
