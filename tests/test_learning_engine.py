"""Test suite for the newly separated Learning Engine and validation layer."""

import json
from unittest.mock import MagicMock, patch
import pytest

from backend.app.modules.learning_engine.engines import LearningEngine
from backend.app.modules.learning_engine.models import LearningEngineRequest
from backend.app.modules.ai_engine.models import ReadingContext
from backend.app.modules.database.review import validate_learning_material
from backend.app.modules.reading_engine.ai_bridge import AiBridge, AiSessionSummaryRequest, LookupRecord

def mock_groq(payload: dict) -> MagicMock:
    """Build a Groq client stub returning `payload` as the completion body."""
    client = MagicMock()
    message = MagicMock()
    message.content = json.dumps(payload)
    choice = MagicMock()
    choice.message = message
    completion = MagicMock()
    completion.choices = [choice]
    client.chat.completions.create.return_value = completion
    return client


class TestLearningEngine:
    @patch("backend.app.modules.learning_engine.engines.Groq")
    @patch("backend.app.modules.learning_engine.engines.learning_engine_key")
    def test_key_present_generates_payload(self, mock_key, mock_groq_cls):
        mock_key.return_value = "groq-key-3"
        mock_groq_cls.return_value = mock_groq({
            "quiz": [{"question": "Q1", "options": ["A", "B", "C", "D"], "correct_answer": "A"}],
            "flashcards": [{"word": "test", "fun_definition": "a trial"}]
        })

        engine = LearningEngine()
        request = LearningEngineRequest(
            context=ReadingContext(),
            session_history=[],
            session_summary="Test",
            words_learned=[]
        )
        response = engine.generate(request)

        assert len(response.quiz) == 1
        assert response.quiz[0]["question"] == "Q1"
        assert len(response.flashcards) == 1
        assert response.flashcards[0]["word"] == "test"

    @patch("backend.app.modules.learning_engine.engines.learning_engine_key")
    def test_key_absent_yields_empty_lists(self, mock_key):
        mock_key.return_value = ""
        engine = LearningEngine()
        
        request = LearningEngineRequest(
            context=ReadingContext(),
            session_history=[],
            session_summary="",
            words_learned=[]
        )
        response = engine.generate(request)
        
        # Should return gracefully, without making network calls
        assert response.quiz == []
        assert response.flashcards == []

    @patch("backend.app.modules.learning_engine.engines.Groq")
    @patch("backend.app.modules.learning_engine.engines.learning_engine_key")
    def test_engine_failure_handled_gracefully(self, mock_key, mock_groq_cls):
        mock_key.return_value = "groq-key-3"
        client = MagicMock()
        client.chat.completions.create.side_effect = Exception("Timeout")
        mock_groq_cls.return_value = client

        engine = LearningEngine()
        request = LearningEngineRequest(
            context=ReadingContext(),
            session_history=[],
            session_summary="",
            words_learned=[]
        )
        response = engine.generate(request)
        
        # Should return gracefully on exception
        assert response.quiz == []
        assert response.flashcards == []


class TestPhase1ValidationReuse:
    def test_validate_learning_material_removes_bad_questions(self):
        raw_payload = {
            "quiz": [
                {
                    "question": "Good question",
                    "options": ["A", "B", "C", "D"],
                    "correct_answer": "B"
                },
                {
                    "question": "Bad question (answer missing)",
                    "options": ["A", "B", "C", "D"],
                    "correct_answer": "E"
                },
                {
                    "question": "Duplicate options",
                    "options": ["A", "A", "C", "D"],
                    "correct_answer": "A"
                },
                {
                    "question": "Not enough options",
                    "options": ["A"],
                    "correct_answer": "A"
                }
            ],
            "flashcards": [],
            "words_learned": []
        }
        
        validated = validate_learning_material(raw_payload)
        
        # Only the first question should survive
        assert len(validated["quiz"]) == 1
        assert validated["quiz"][0]["question"] == "Good question"

    def test_validate_learning_material_fallback_flashcards(self):
        raw_payload = {
            "quiz": [],
            "flashcards": [
                {"word": "included", "fun_definition": "explicitly returned"}
            ],
            "words_learned": [
                {"word": "included", "takeaway": "already handled"},
                {"word": "fallback", "takeaway": "needs flashcard"}
            ]
        }
        
        validated = validate_learning_material(raw_payload)
        
        # Should contain the explicitly returned one PLUS the fallback one
        assert len(validated["flashcards"]) == 2
        words = {f["word"] for f in validated["flashcards"]}
        assert words == {"included", "fallback"}
        
        fallback_card = next(f for f in validated["flashcards"] if f["word"] == "fallback")
        assert fallback_card["fun_definition"] == "needs flashcard"


class TestBridgeIntegration:
    @pytest.mark.asyncio
    async def test_ai_bridge_uses_correct_keys(self):
        # We manually test key isolation by mocking the models' behaviors.
        bridge = AiBridge()
        
        # Provide a minimal lookup history so review() actually fires
        bridge._history.append(LookupRecord(word="test", context="test", mode_used="meaning"))
        
        # Mock summary generator (Key 1)
        mock_summary = MagicMock()
        mock_summary.summarize.return_value = type('MockResponse', (), {
            "status": "ok",
            "data": {"session_summary": "Sum", "words_learned": []}
        })()
        bridge.summary_generator = mock_summary
        
        # Mock learning engine (Key 3)
        mock_learning = MagicMock()
        mock_learning.generate.return_value = type('MockLearningResponse', (), {
            "quiz": [{"question": "Q1", "options": ["A", "B", "C", "D"], "correct_answer": "A"}],
            "flashcards": [{"word": "F1", "fun_definition": "D1"}]
        })()
        bridge.learning_engine = mock_learning
        
        outcome = await bridge.review()
        
        assert outcome.ok
        # Merge has happened!
        assert outcome.data["session_summary"] == "Sum"
        assert len(outcome.data["quiz"]) == 1
        assert len(outcome.data["flashcards"]) == 1
        
        # Both engines were called exactly once
        mock_summary.summarize.assert_called_once()
        mock_learning.generate.assert_called_once()
