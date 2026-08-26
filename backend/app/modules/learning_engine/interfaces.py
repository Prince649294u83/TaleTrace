from typing import Protocol

from .models import LearningCapabilityResponse, LearningEngineRequest


class LearningEngineInterface(Protocol):
    def generate(self, request: LearningEngineRequest) -> LearningCapabilityResponse:
        """Generate quiz and flashcards. Returns empty lists on failure."""
        ...
