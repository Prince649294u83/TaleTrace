"""Static AI Engine placeholders with no inference or external calls."""

from backend.app.modules.ai_engine.models import AiInput, AiPlaceholderResponse


class PlaceholderCapability:
    """Base placeholder adapter shared by capability shells."""

    capability = "ai_engine"

    def _response(self) -> AiPlaceholderResponse:
        return AiPlaceholderResponse(capability=self.capability)


class ExplanationEnginePlaceholder(PlaceholderCapability):
    capability = "explanation_engine"

    def explain(self, _request: AiInput) -> AiPlaceholderResponse:
        return self._response()


class AdaptiveReadingPlaceholder(PlaceholderCapability):
    capability = "adaptive_reading"

    def adapt(self, _request: AiInput) -> AiPlaceholderResponse:
        return self._response()


class NovelModePlaceholder(PlaceholderCapability):
    capability = "novel_mode"

    def create(self, _request: AiInput) -> AiPlaceholderResponse:
        return self._response()


class ImageDecisionPlaceholder(PlaceholderCapability):
    capability = "image_decision"

    def decide(self, _request: AiInput) -> AiPlaceholderResponse:
        return self._response()


class SummaryGeneratorPlaceholder(PlaceholderCapability):
    capability = "summary_generator"

    def summarize(self, _request: AiInput) -> AiPlaceholderResponse:
        return self._response()


class FlashcardGeneratorPlaceholder(PlaceholderCapability):
    capability = "flashcard_generator"

    def generate(self, _request: AiInput) -> AiPlaceholderResponse:
        return self._response()


class QuizGeneratorPlaceholder(PlaceholderCapability):
    capability = "quiz_generator"

    def generate(self, _request: AiInput) -> AiPlaceholderResponse:
        return self._response()
