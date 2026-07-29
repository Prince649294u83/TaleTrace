"""AI Engine module boundary."""

from backend.app.modules.ai_engine.interfaces import (
    AdaptiveReadingInterface,
    ExplanationEngineInterface,
    FlashcardGeneratorInterface,
    ImageDecisionInterface,
    NovelModeInterface,
    PromptBuilderInterface,
    QuizGeneratorInterface,
    SummaryGeneratorInterface,
)
from backend.app.modules.ai_engine.models import AiInput, AiPlaceholderResponse

__all__ = [
    "AdaptiveReadingInterface",
    "AiInput",
    "AiPlaceholderResponse",
    "ExplanationEngineInterface",
    "FlashcardGeneratorInterface",
    "ImageDecisionInterface",
    "NovelModeInterface",
    "PromptBuilderInterface",
    "QuizGeneratorInterface",
    "SummaryGeneratorInterface",
]