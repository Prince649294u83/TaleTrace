import json
from dataclasses import dataclass
from typing import Any

from backend.app.modules.ai_engine.models import ReadingContext
from backend.app.modules.ai_engine.prompts import JSON_RULES


@dataclass
class PromptBuilder:
    """Builds prompts for the Learning Engine's educational payload generation."""

    def book_description(self, context: ReadingContext) -> str:
        """Describes the reading context so the tone matches the audience."""
        book = context.book
        if not book.title:
            return ""

        parts = [f"The reader is reading '{book.title}'"]
        if book.author:
            parts.append(f"by {book.author}")
        parts.append(".")

        if book.audience:
            parts.append(f"It is at a {book.audience} level.")
        if book.genre:
            parts.append(f"The genre is {book.genre}.")

        return " ".join(parts)

    def learning_material_prompt(
        self,
        context: ReadingContext,
        session_history: list[dict[str, Any]],
        session_summary: str,
        words_learned: list[dict[str, str]],
    ) -> str:
        """The strict prompt for generating quizzes and flashcards.

        Crucially, instructs the LLM to only use facts from `session_history`.
        """

        history_str = json.dumps(session_history, indent=2)
        vocab_str = json.dumps(words_learned, indent=2)

        return (
            "You are an educational assistant. The reader has just finished a reading session.\n\n"
            f"{self.book_description(context)}\n\n"
            "Below is the exact history of passages read during this session:\n"
            f"```json\n{history_str}\n```\n\n"
            "Below is the vocabulary the reader learned during this session:\n"
            f"```json\n{vocab_str}\n```\n\n"
            "Generate flashcards and a multiple-choice comprehension quiz based on this session.\n\n"
            "RULES:\n"
            "1. Match the tone to the book's genre and audience; be encouraging.\n"
            "2. **STRICT CONSTRAINT**: You MUST ONLY derive quiz questions and flashcard hints from "
            "facts explicitly present in the provided session history or vocabulary. Do NOT invent facts "
            "or use external knowledge about the book.\n"
            "3. hint_from_story must reference specific events or characters from the passages in the history.\n"
            "4. Quizzes should test understanding of the passage, not just word recall.\n"
            "5. Generate a flashcard for each word in the vocabulary list.\n\n"
            "Output this JSON structure exactly:\n"
            "{\n"
            '  "flashcards": [{"word": "string", "hint_from_story": "string", "fun_definition": "string"}],\n'
            '  "quiz": [{"question": "string", "options": ["string","string","string","string"], "correct_answer": "string", "feedback": "string"}]\n'
            "}\n\n"
            f"{JSON_RULES}"
        )
