"""Reusable prompt construction for the AI Engine.

All engines build their prompts here so the shared JSON rules, book metadata,
and reading-context framing live in exactly one place.
"""

from backend.app.modules.ai_engine.models import ReadingContext, ReadingMode

JSON_RULES = (
    "Respond with valid JSON only. No prose, markdown, or code fences outside "
    "the JSON. Use colons between keys and values, never equals signs, and no "
    "trailing commas."
)

PERSONA = (
    "You are TaleTrace, a smart reading companion that helps a reader "
    "understand what is in front of them right now."
)

_MODE_GUIDANCE: dict[ReadingMode, str] = {
    ReadingMode.STANDARD: "Keep the explanation clear and natural.",
    ReadingMode.ADAPTIVE: (
        "Use simple language, a relatable analogy, and explain step-by-step as "
        "if teaching a beginner."
    ),
    ReadingMode.DISABILITY: (
        "Use short sentences and everyday words. Prefer one idea per sentence "
        "and avoid figurative language."
    ),
    ReadingMode.NOVEL: (
        "Stay immersive and keep the story's mood intact; avoid breaking the "
        "narrative spell."
    ),
    ReadingMode.STUDY: (
        "Be precise and define the term properly, then connect it back to the "
        "passage. Note distinctions that matter for understanding."
    ),
    ReadingMode.EXAM: (
        "Be concise and factual. Lead with the definition that would be "
        "expected in an answer, then one line of context."
    ),
}


class PromptBuilder:
    """Builds system prompts for each AI capability from a ReadingContext."""

    def book_description(self, context: ReadingContext) -> str:
        """Describe the book generically, using whatever metadata exists."""

        book = context.book
        parts: list[str] = []
        if book.title:
            parts.append(f"Book: {book.title}")
        if book.author:
            parts.append(f"Author: {book.author}")
        if book.genre:
            parts.append(f"Genre: {book.genre}")
        if book.chapter:
            parts.append(f"Chapter: {book.chapter}")
        if book.audience:
            parts.append(f"Audience: {book.audience}")

        if not parts:
            return (
                "The book is unknown. Infer tone and reading level from the "
                "passage itself."
            )
        return "The reader is currently reading:\n" + "\n".join(parts)

    def mode_guidance(self, context: ReadingContext) -> str:
        return _MODE_GUIDANCE.get(context.mode, _MODE_GUIDANCE[ReadingMode.STANDARD])

    def session_memory(self, context: ReadingContext) -> str:
        """Render earlier lookups so the AI can build on prior concepts."""

        if not context.previously_explained:
            return ""
        recap = "; ".join(item.recap() for item in context.previously_explained)
        return (
            f"\n\nThe reader has already learned these words this session: {recap}. "
            "Reference one only if there is a genuinely natural, clear connection "
            "to the current word (same character, similar concept, or a direct "
            "cause-effect relationship). If no natural connection exists, explain "
            "the new word entirely on its own — never invent a forced link."
        )

    def user_content(self, context: ReadingContext) -> str:
        """Render the reading state the model should reason over."""

        lines: list[str] = []
        if context.selected_word:
            lines.append(f"Selected word: {context.selected_word}")
        if context.page_number is not None:
            lines.append(f"Page: {context.page_number}")
        if context.previous_paragraph:
            lines.append(f"Previous paragraph: {context.previous_paragraph}")
        if context.current_paragraph:
            lines.append(f"Current paragraph: {context.current_paragraph}")
        lines.append(f"Reading mode: {context.mode.value}")
        return "\n".join(lines)

    def explanation_prompt(self, context: ReadingContext) -> str:
        return (
            f"{PERSONA} You explain words using their surrounding paragraph "
            "context — never a plain dictionary definition.\n\n"
            f"{self.book_description(context)}\n\n"
            f"{self.mode_guidance(context)}\n\n"
            "Output this JSON structure:\n"
            '{"oled_text": "a short phrase, under 12 words, for a tiny display", '
            '"full_explanation": "2-3 sentences explaining the word in context, '
            'natural enough to be read aloud", '
            '"difficulty_level": "beginner" | "intermediate" | "advanced"}\n\n'
            "difficulty_level describes how hard the word itself is for this "
            "reader, given the book's audience.\n\n"
            f"{JSON_RULES}"
            f"{self.session_memory(context)}"
        )

    def image_decision_prompt(self, context: ReadingContext) -> str:
        return (
            f"{PERSONA} Decide whether showing a supporting image would "
            "genuinely help this reader understand the selected word better.\n\n"
            f"{self.book_description(context)}\n\n"
            "GUIDANCE:\n"
            "- Concrete, visual subjects (creatures, objects, places, "
            "mechanisms, locations) often benefit from an image.\n"
            "- Abstract concepts, emotions, and manners of action rarely do — a "
            "good explanation is enough and an image may confuse.\n"
            "- Default to not showing an image unless it clearly adds "
            "understanding beyond the text explanation.\n\n"
            "Output this JSON structure:\n"
            '{"show_image": true or false, '
            '"image_query": "a short, simple search phrase, or empty string", '
            '"image_type": "illustration" | "diagram" | "photo" | "map" | '
            '"portrait" | "none", '
            '"reason": "one short sentence explaining the decision"}\n\n'
            "image_type tells the retrieval layer what style of image to fetch; "
            'use "none" when show_image is false.\n\n'
            f"{JSON_RULES}"
        )

    def novel_mode_prompt(self, context: ReadingContext, audio_tags: list[str]) -> str:
        tag_list = "\n".join(f"- {tag}" for tag in audio_tags)
        return (
            f"{PERSONA} The reader is in Novel Mode, where ambient audio "
            "follows the scene.\n\n"
            f"{self.book_description(context)}\n\n"
            "Choose exactly ONE audio_tag for the ambient background from this list:\n"
            f"{tag_list}\n\n"
            'Use "neutral_narration" for connective narration or anything that '
            "does not clearly fit.\n\n"
            "Also report a brief scene_mood (e.g. 'A quiet conversation'), the dominant emotion (a single lowercase word such as "
            "calm, tension, excitement, sadness, wonder, humour) and an "
            "intensity from 0.0 to 1.0 describing how strongly the scene "
            "carries that emotion, so audio can fade smoothly between scenes.\n\n"
            "Output this JSON structure:\n"
            '{"scene_mood": "string", "audio_tag": "string", "emotion": "string", '
            '"intensity": 0.0, '
            '"companion_commentary": "one sentence under 15 words for the OLED"}\n\n'
            f"{JSON_RULES}"
        )

    def summary_prompt(self, context: ReadingContext) -> str:
        return (
            f"{PERSONA} The reader has just finished a reading session.\n\n"
            f"{self.book_description(context)}\n\n"
            "Generate an engaging review from the session history: the list of "
            "words learned, and a short summary.\n\n"
            "RULES:\n"
            "1. Match the tone to the book's genre and audience; be "
            "encouraging.\n"
            "2. words_learned must list every distinct word from the session "
            "history, each with a one-line takeaway.\n"
            "3. Keep session_summary to 1-2 sentences.\n\n"
            "Output this JSON structure:\n"
            "{"
            '"words_learned": [{"word": "string", "takeaway": "string"}], '
            '"session_summary": "string"'
            "}\n\n"
            f"{JSON_RULES}"
        )
