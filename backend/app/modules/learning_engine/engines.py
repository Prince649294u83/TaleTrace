import json
import logging
from groq import Groq

from backend.app.shared.groq_keys import learning_engine_key, chat_model
from backend.app.modules.ai_engine.engines import _respond
from .interfaces import LearningEngineInterface
from .models import LearningEngineRequest, LearningCapabilityResponse
from .prompts import PromptBuilder


class LearningEngine(LearningEngineInterface):
    """Generates educational materials using GROQ_API_KEY_3 exclusively.
    
    Failures (missing key, timeout, malformed JSON) are caught and logged,
    degrading gracefully to empty quizzes and flashcards rather than aborting
    the session.
    """

    def __init__(self):
        self.prompts = PromptBuilder()
        self.key = learning_engine_key()

    def generate(self, request: LearningEngineRequest) -> LearningCapabilityResponse:
        if not self.key:
            logging.warning("Learning Engine: GROQ_API_KEY_3 missing. Quiz/Flashcards skipped.")
            return LearningCapabilityResponse(flashcards=[], quiz=[])

        prompt = self.prompts.learning_material_prompt(
            context=request.context,
            session_history=request.session_history,
            session_summary=request.session_summary,
            words_learned=request.words_learned,
        )

        try:
            client = Groq(api_key=self.key)
            completion = client.chat.completions.create(
                model=chat_model(),
                messages=[
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                max_tokens=2048,
                response_format={"type": "json_object"},
            )

            content = completion.choices[0].message.content or "{}"
            result = json.loads(content)
            
            return LearningCapabilityResponse(
                flashcards=result.get("flashcards", []),
                quiz=result.get("quiz", [])
            )
        except Exception as e:
            logging.error(f"Learning Engine generation failed: {e}")
            return LearningCapabilityResponse(flashcards=[], quiz=[])
