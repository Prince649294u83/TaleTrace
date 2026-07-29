"""AI Engine capability interfaces; no LLM or provider dependency."""

from typing import Protocol

from backend.app.modules.ai_engine.models import AiInput, AiPlaceholderResponse


class PromptBuilderInterface(Protocol):
    def build(self, request: AiInput) -> AiPlaceholderResponse:
        """Return a placeholder for future prompt construction."""
        ...


class ExplanationEngineInterface(Protocol):
    def explain(self, request: AiInput) -> AiPlaceholderResponse:
        """Return a placeholder for future explanation generation."""
        ...


class AdaptiveReadingInterface(Protocol):
    def adapt(self, request: AiInput) -> AiPlaceholderResponse:
        """Return a placeholder for future adaptive reading."""
        ...


class NovelModeInterface(Protocol):
    def create(self, request: AiInput) -> AiPlaceholderResponse:
        """Return a placeholder for future novel mode generation."""
        ...


class ImageDecisionInterface(Protocol):
    def decide(self, request: AiInput) -> AiPlaceholderResponse:
        """Return a placeholder for future image decisioning."""
        ...


class SummaryGeneratorInterface(Protocol):
    def summarize(self, request: AiInput) -> AiPlaceholderResponse:
        """Return a placeholder for future summary generation."""
        ...


class FlashcardGeneratorInterface(Protocol):
    def generate(self, request: AiInput) -> AiPlaceholderResponse:
        """Return a placeholder for future flashcard generation."""
        ...


class QuizGeneratorInterface(Protocol):
    def generate(self, request: AiInput) -> AiPlaceholderResponse:
        """Return a placeholder for future quiz generation."""
        ...
