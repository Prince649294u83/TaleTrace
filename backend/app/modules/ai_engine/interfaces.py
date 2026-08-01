"""AI Engine capability interfaces; no LLM or provider dependency."""

from typing import Protocol

from backend.app.modules.ai_engine.models import (
    AiExplainRequest,
    AiInput,
    AiPlaceholderResponse,
    AiSessionSummaryRequest,
    ReadingContext,
)


class PromptBuilderInterface(Protocol):
    def explanation_prompt(self, context: ReadingContext) -> str:
        """Return the system prompt for context-aware word explanation."""
        ...

    def image_decision_prompt(self, context: ReadingContext) -> str:
        """Return the system prompt for the image-decision capability."""
        ...

    def novel_mode_prompt(self, context: ReadingContext, scene_moods: list[str]) -> str:
        """Return the system prompt for Novel Mode scene classification."""
        ...

    def summary_prompt(self, context: ReadingContext) -> str:
        """Return the system prompt for session-end review generation."""
        ...

    def user_content(self, context: ReadingContext) -> str:
        """Render the reading state the model should reason over."""
        ...


class ExplanationEngineInterface(Protocol):
    def explain(self, request: AiExplainRequest) -> AiPlaceholderResponse:
        """Explain the selected word using its reading context."""
        ...


class AdaptiveReadingInterface(Protocol):
    def adapt(self, request: AiInput) -> AiPlaceholderResponse:
        """Return a placeholder for future adaptive reading."""
        ...


class NovelModeInterface(Protocol):
    def create(self, request: AiInput) -> AiPlaceholderResponse:
        """Classify the passage into a scene mood, emotion, and intensity."""
        ...


class ImageDecisionInterface(Protocol):
    def decide(self, request: AiInput) -> AiPlaceholderResponse:
        """Decide whether a supporting image helps, and what kind to fetch."""
        ...


class SummaryGeneratorInterface(Protocol):
    def summarize(self, request: AiSessionSummaryRequest) -> AiPlaceholderResponse:
        """Generate flashcards, quiz, words learned, and a session summary."""
        ...


class FlashcardGeneratorInterface(Protocol):
    def generate(self, request: AiInput) -> AiPlaceholderResponse:
        """Return a placeholder for future flashcard generation."""
        ...


class QuizGeneratorInterface(Protocol):
    def generate(self, request: AiInput) -> AiPlaceholderResponse:
        """Return a placeholder for future quiz generation."""
        ...
