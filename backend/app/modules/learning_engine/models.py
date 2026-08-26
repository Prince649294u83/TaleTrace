from dataclasses import dataclass
from typing import Any

from backend.app.modules.ai_engine.models import ReadingContext


@dataclass(frozen=True)
class LearningEngineRequest:
    """The inputs required to generate educational materials for a session."""

    context: ReadingContext
    session_history: list[dict[str, str]]
    session_summary: str
    words_learned: list[dict[str, str]]


@dataclass(frozen=True)
class LearningCapabilityResponse:
    """The raw output from the Learning Engine's LLM generation.
    
    Contains unvalidated dictionaries exactly as output by the model. 
    They must be passed through `review.validate_learning_material` before use.
    """

    flashcards: list[dict[str, Any]]
    quiz: list[dict[str, Any]]
