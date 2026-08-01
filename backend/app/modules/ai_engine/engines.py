"""
Real AI Engine implementations (Groq-backed) matching the Protocol interfaces
defined in interfaces.py. These replace the static placeholders in
placeholders.py once wired into router.py.
"""

import os
import json
from groq import Groq
from dotenv import load_dotenv

from backend.app.modules.ai_engine.models import (
    AiInput,
    AiExplainRequest,
    AiPlaceholderResponse,
    AiSessionSummaryRequest,
)

load_dotenv()
_client = Groq(api_key=os.getenv("GROQ_API_KEY"))


def _safe_json_completion(system_prompt: str, user_content: str, max_retries: int = 2) -> dict:
    """Shared retry wrapper: Groq occasionally emits malformed JSON on complex schemas."""
    last_error = None
    for _ in range(max_retries + 1):
        try:
            response = _client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                response_format={"type": "json_object"},
            )
            return json.loads(response.choices[0].message.content)
        except Exception as e:
            last_error = e
            continue
    return {"error": f"Failed after {max_retries + 1} attempts: {str(last_error)}"}


class ExplanationEngine:
    """Implements ExplanationEngineInterface. Expects request.text = paragraph,
    request.metadata = {"word": "...", "mode": "standard" | "adaptive"}.
    Optionally request.previously_explained for progressive Adaptive learning."""

    capability = "explanation_engine"

    def explain(self, request: AiExplainRequest) -> AiPlaceholderResponse:
        word = request.metadata.get("word", "")
        mode = request.metadata.get("mode", "standard")
        paragraph = request.text or ""
        previously_explained = getattr(request, "previously_explained", [])

        system_prompt = (
            "You are TaleTrace's reading companion. You explain words using their "
            "surrounding paragraph context — never a plain dictionary definition.\n\n"
            "Respond ONLY in valid JSON with this exact structure:\n"
            '{"oled_text": "a short phrase, under 12 words, for a tiny display", '
            '"full_explanation": "2-3 sentences explaining the word in context, '
            'natural enough to be read aloud"}\n\n'
            "Do not include any text outside the JSON."
        )

        if mode == "adaptive":
            system_prompt += (
                "\n\nUse simple language, a relatable analogy, and explain "
                "step-by-step as if teaching a beginner."
            )

            if previously_explained:
                recap = "; ".join(
                    f"'{item['word']}' meaning {item.get('fun_definition', item.get('back', ''))}"
                    for item in previously_explained
                )
                system_prompt += (
                    f"\n\nThe reader has already learned these words this session: {recap}. "
                    "ONLY reference one of these if there's a genuinely natural, clear connection "
                    "to the current word (e.g. same character, similar concept, or a direct "
                    "cause-effect relationship in the story). If no natural connection exists, "
                    "explain the new word completely on its own — do NOT invent a forced or "
                    "confusing link just to make a connection."
                )

        result = _safe_json_completion(
            system_prompt, f"Word: '{word}'. Paragraph: '{paragraph}'"
        )

        return AiPlaceholderResponse(
            status="ok" if "error" not in result else "error",
            capability=self.capability,
            message=json.dumps(result),
        )


class ImageDecision:
    """Implements ImageDecisionInterface. Decides whether a supporting image
    would help explain the word, and if so, what to search for. Does NOT
    fetch the image itself — that's a separate retrieval concern."""

    capability = "image_decision"

    def decide(self, request: AiInput) -> AiPlaceholderResponse:
        word = request.metadata.get("word", "")
        paragraph = request.text or ""

        system_prompt = """You are TaleTrace's image-decision assistant. Given a word and its paragraph context from "Septopus: Trouble on the High Cs", decide whether showing a supporting image would genuinely help a young reader understand the word better.

GUIDANCE:
- Concrete, visual nouns (creatures, objects, places, actions with a clear visual form) often benefit from an image.
- Abstract concepts, emotions, or feelings (e.g. "furious", "peacefully", "sabotage") almost never need an image — a good explanation is enough and an image could be confusing or unhelpful.
- Only recommend an image if it adds real understanding beyond the text explanation. Prioritize NOT showing an image unless it clearly helps.

Output ONLY valid JSON:
{
  "show_image": true or false,
  "image_query": "a short, simple search phrase if show_image is true, otherwise empty string",
  "reason": "one short sentence explaining the decision"
}"""

        result = _safe_json_completion(
            system_prompt, f"Word: '{word}'. Paragraph: '{paragraph}'"
        )

        return AiPlaceholderResponse(
            status="ok" if "error" not in result else "error",
            capability=self.capability,
            message=json.dumps(result),
        )


class NovelMode:
    """Implements NovelModeInterface. Expects request.text = paragraph."""

    capability = "novel_mode"

    AUDIO_TAGS = {
        "peaceful_underwater": "sfx_calm_bubbles.mp3",
        "musical_rehearsal": "sfx_quirky_orchestra.mp3",
        "suspense_villain": "sfx_sneaky_caper.mp3",
        "action_rescue": "sfx_upbeat_adventure.mp3",
        "comedy_confusion": "sfx_bouncing_comedy.mp3",
        "neutral_narration": "sfx_soft_ambient.mp3",
    }

    def create(self, request: AiInput) -> AiPlaceholderResponse:
        paragraph = request.text or ""

        system_prompt = """You are the AI brain of TaleTrace, a smart reading companion. The user is currently reading "Septopus: Trouble on the High Cs" in Novel Mode.

Classify the given paragraph into exactly ONE of these scene_moods:
1. "peaceful_underwater" (general Sea World life, swimming, calm dialogue)
2. "musical_rehearsal" (Oct-estra practice, instruments, concert prep)
3. "suspense_villain" (Jai Kalia's sabotage plots, kidnapping, yacht scenes)
4. "action_rescue" (Rot8 and Tumboo's rescue mission, chases, high stakes)
5. "comedy_confusion" (funny mispronunciations, mistaken kidnapping, slapstick)
6. "neutral_narration" (connective narration or anything that doesn't clearly fit)

Also write a 1-sentence companion_commentary (under 15 words) for the OLED.

Output ONLY valid JSON: {"scene_mood": "string", "companion_commentary": "string"}"""

        result = _safe_json_completion(system_prompt, f"Text: {paragraph}")

        if "error" not in result:
            if result.get("scene_mood") not in self.AUDIO_TAGS:
                result["scene_mood"] = "neutral_narration"
            result["audio_tag"] = self.AUDIO_TAGS[result["scene_mood"]]

        return AiPlaceholderResponse(
            status="ok" if "error" not in result else "error",
            capability=self.capability,
            message=json.dumps(result),
        )


class SummaryGenerator:
    """Implements SummaryGeneratorInterface (covers flashcards + quiz + summary
    together for now). Expects an AiSessionSummaryRequest with a real
    session_history list field."""

    capability = "summary_generator"

    def summarize(self, request: AiSessionSummaryRequest) -> AiPlaceholderResponse:
        session_history = request.session_history

        if not session_history:
            return AiPlaceholderResponse(
                status="error",
                capability=self.capability,
                message=json.dumps({"error": "No lookups recorded this session"}),
            )

        history_text = "\n".join(
            f"- Word: '{item['word']}' | Context: \"{item['context']}\" | Mode used: {item['mode_used']}"
            for item in session_history
        )

        system_prompt = """You are the AI Learning Companion for TaleTrace. The user has just finished a reading session of "Septopus: Trouble on the High Cs".

Generate a fun, engaging review consisting of Flashcards and a Multiple-Choice Quiz based on session_history.

TONE & STYLE RULES:
1. Target audience: middle-grade readers. Encouraging, fun, adventurous tone.
2. Story-tied flashcards: hint_from_story must reference specific book events/characters.
3. Comprehension quizzes: test story understanding, not just word recall.
4. Include a 1-2 sentence session_summary.

CRITICAL FORMATTING RULE: use colons between key and value, never equals signs. Strictly valid JSON, no trailing commas.

Output ONLY valid JSON:
{
  "flashcards": [{"word": "string", "hint_from_story": "string", "fun_definition": "string"}],
  "quiz": [{"question": "string", "options": ["string","string","string","string"], "correct_answer": "string", "feedback": "string"}],
  "session_summary": "string"
}"""

        result = _safe_json_completion(system_prompt, f"session_history:\n{history_text}")

        return AiPlaceholderResponse(
            status="ok" if "error" not in result else "error",
            capability=self.capability,
            message=json.dumps(result),
        )