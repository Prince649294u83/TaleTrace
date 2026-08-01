"""Engine and route tests with a mocked Groq client (no network calls)."""

import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.modules.ai_engine.engines import (
    ExplanationEngine,
    ImageDecision,
    NovelMode,
    SummaryGenerator,
    _clamp_intensity,
)
from backend.app.modules.ai_engine.models import (
    AiExplainRequest,
    AiInput,
    AiSessionSummaryRequest,
    LookupRecord,
)


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


@pytest.fixture
def client():
    return TestClient(app)


class TestNormalizers:
    """Output normalization guards against malformed model responses."""

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("Beginner", "beginner"),
            ("ADVANCED", "advanced"),
            ("intermediate", "intermediate"),
            ("nonsense", "intermediate"),
            (None, "intermediate"),
            (42, "intermediate"),
        ],
    )
    def test_difficulty_normalization(self, value, expected):
        assert ExplanationEngine._normalize_difficulty(value) == expected

    @pytest.mark.parametrize(
        "value,show,expected",
        [
            ("diagram", True, "diagram"),
            ("MAP", True, "map"),
            ("none", True, "illustration"),
            ("bogus", True, "illustration"),
            (None, True, "illustration"),
            ("photo", False, "none"),
        ],
    )
    def test_image_type_normalization(self, value, show, expected):
        assert ImageDecision._normalize_image_type(value, show) == expected

    @pytest.mark.parametrize(
        "value,expected",
        [(0.84, 0.84), ("0.5", 0.5), (1.9, 1.0), (-3, 0.0), (None, 0.5), ("abc", 0.5)],
    )
    def test_intensity_clamping(self, value, expected):
        assert _clamp_intensity(value) == expected


class TestExplanationEngine:
    def test_returns_normalized_payload(self):
        payload = {
            "oled_text": "a collection",
            "full_explanation": "A portfolio is a collection of things you hold.",
            "difficulty_level": "Advanced",
        }
        with patch(
            "backend.app.modules.ai_engine.engines._get_client",
            return_value=mock_groq(payload),
        ):
            result = ExplanationEngine().explain(
                AiExplainRequest(text="Your portfolio grew.", metadata={"word": "portfolio"})
            )
        assert result.status == "ok"
        assert result.data["difficulty_level"] == "advanced"
        assert result.data["oled_text"] == "a collection"
        # message stays a JSON string for earlier callers
        assert json.loads(result.message) == result.data

    def test_missing_fields_get_defaults(self):
        with patch(
            "backend.app.modules.ai_engine.engines._get_client",
            return_value=mock_groq({"oled_text": "hi"}),
        ):
            result = ExplanationEngine().explain(
                AiExplainRequest(text="p", metadata={"word": "w"})
            )
        assert result.data["full_explanation"] == ""
        assert result.data["difficulty_level"] == "intermediate"

    def test_api_failure_returns_error_status(self):
        client = MagicMock()
        client.chat.completions.create.side_effect = RuntimeError("boom")
        with patch(
            "backend.app.modules.ai_engine.engines._get_client", return_value=client
        ):
            result = ExplanationEngine().explain(
                AiExplainRequest(text="p", metadata={"word": "w"})
            )
        assert result.status == "error"
        assert "error" in result.data

    def test_prompt_is_book_agnostic(self):
        """The book in the prompt must come from metadata, not be hardcoded."""
        groq = mock_groq({"oled_text": "x", "full_explanation": "y"})
        with patch(
            "backend.app.modules.ai_engine.engines._get_client", return_value=groq
        ):
            ExplanationEngine().explain(
                AiExplainRequest(
                    text="Habits compound.",
                    metadata={"word": "compound", "book_title": "Atomic Habits"},
                )
            )
        sent = groq.chat.completions.create.call_args.kwargs["messages"][0]["content"]
        assert "Atomic Habits" in sent
        assert "Septopus" not in sent


class TestImageDecision:
    def test_show_image_true(self):
        payload = {
            "show_image": True,
            "image_query": "giant octopus",
            "image_type": "photo",
            "reason": "A visual helps.",
        }
        with patch(
            "backend.app.modules.ai_engine.engines._get_client",
            return_value=mock_groq(payload),
        ):
            result = ImageDecision().decide(
                AiInput(text="The octopus swam.", metadata={"word": "octopus"})
            )
        assert result.data["show_image"] is True
        assert result.data["image_type"] == "photo"

    def test_show_image_false_clears_query_and_type(self):
        payload = {
            "show_image": False,
            "image_query": "leftover query",
            "image_type": "diagram",
            "reason": "Abstract word.",
        }
        with patch(
            "backend.app.modules.ai_engine.engines._get_client",
            return_value=mock_groq(payload),
        ):
            result = ImageDecision().decide(
                AiInput(text="He was furious.", metadata={"word": "furious"})
            )
        assert result.data["show_image"] is False
        assert result.data["image_query"] == ""
        assert result.data["image_type"] == "none"


class TestNovelMode:
    def test_valid_mood_maps_to_audio_tag(self):
        payload = {
            "scene_mood": "action",
            "emotion": "Excitement",
            "intensity": 0.84,
            "companion_commentary": "The rescue begins!",
        }
        with patch(
            "backend.app.modules.ai_engine.engines._get_client",
            return_value=mock_groq(payload),
        ):
            result = NovelMode().create(AiInput(text="They raced to the rescue."))
        assert result.data["scene_mood"] == "action"
        assert result.data["emotion"] == "excitement"
        assert result.data["intensity"] == 0.84
        assert result.data["audio_tag"] == NovelMode.AUDIO_TAGS["action"]

    def test_unknown_mood_falls_back_to_neutral(self):
        payload = {"scene_mood": "invented_mood", "emotion": "x", "intensity": 0.3}
        with patch(
            "backend.app.modules.ai_engine.engines._get_client",
            return_value=mock_groq(payload),
        ):
            result = NovelMode().create(AiInput(text="Some text."))
        assert result.data["scene_mood"] == "neutral_narration"
        assert result.data["audio_tag"] == NovelMode.AUDIO_TAGS["neutral_narration"]

    def test_every_mood_has_an_audio_tag(self):
        for mood in NovelMode.SCENE_MOODS:
            assert mood in NovelMode.AUDIO_TAGS


class TestSummaryGenerator:
    def test_empty_history_is_an_error(self):
        result = SummaryGenerator().summarize(AiSessionSummaryRequest(session_history=[]))
        assert result.status == "error"
        assert "No lookups" in result.data["error"]

    def test_words_learned_present(self):
        payload = {
            "flashcards": [],
            "quiz": [],
            "words_learned": [{"word": "portfolio", "takeaway": "a collection"}],
            "session_summary": "Good work.",
        }
        with patch(
            "backend.app.modules.ai_engine.engines._get_client",
            return_value=mock_groq(payload),
        ):
            result = SummaryGenerator().summarize(
                AiSessionSummaryRequest(
                    session_history=[LookupRecord(word="portfolio", context="ctx")]
                )
            )
        assert result.data["words_learned"][0]["word"] == "portfolio"

    def test_words_learned_falls_back_to_history(self):
        """If the model omits words_learned, derive it from the session history."""
        payload = {"flashcards": [], "quiz": [], "session_summary": "Nice."}
        with patch(
            "backend.app.modules.ai_engine.engines._get_client",
            return_value=mock_groq(payload),
        ):
            result = SummaryGenerator().summarize(
                AiSessionSummaryRequest(
                    session_history=[
                        LookupRecord(word="alpha"),
                        LookupRecord(word="beta"),
                        LookupRecord(word="alpha"),  # duplicate collapses
                    ]
                )
            )
        words = [w["word"] for w in result.data["words_learned"]]
        assert words == ["alpha", "beta"]


class TestRoutes:
    """The HTTP surface Prince's Reading Engine will integrate against."""

    def test_explain_accepts_rich_context(self, client):
        payload = {
            "oled_text": "a collection",
            "full_explanation": "A portfolio is a collection.",
            "difficulty_level": "intermediate",
        }
        with patch(
            "backend.app.modules.ai_engine.engines._get_client",
            return_value=mock_groq(payload),
        ):
            response = client.post(
                "/ai/explain",
                json={
                    "context": {
                        "book": {"title": "Atomic Habits", "genre": "Self Help"},
                        "page_number": 42,
                        "previous_paragraph": "Habits compound.",
                        "current_paragraph": "Your portfolio grows.",
                        "selected_word": "portfolio",
                        "mode": "study",
                        "previously_explained": [
                            {"word": "compound", "definition": "builds on itself"}
                        ],
                    }
                },
            )
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["data"]["data"]["difficulty_level"] == "intermediate"

    def test_explain_accepts_legacy_shape(self, client):
        """Existing callers using text + metadata must keep working."""
        payload = {"oled_text": "x", "full_explanation": "y", "difficulty_level": "beginner"}
        with patch(
            "backend.app.modules.ai_engine.engines._get_client",
            return_value=mock_groq(payload),
        ):
            response = client.post(
                "/ai/explain",
                json={
                    "text": "The octopus swam.",
                    "metadata": {"word": "octopus", "mode": "adaptive"},
                },
            )
        assert response.status_code == 200
        assert response.json()["data"]["status"] == "ok"

    def test_error_surfaces_in_envelope(self, client):
        response = client.post("/ai/session-summary", json={"session_history": []})
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is False
        assert body["errors"] and "No lookups" in body["errors"][0]

    def test_image_decision_route(self, client):
        payload = {
            "show_image": True,
            "image_query": "octopus",
            "image_type": "photo",
            "reason": "Visual noun.",
        }
        with patch(
            "backend.app.modules.ai_engine.engines._get_client",
            return_value=mock_groq(payload),
        ):
            response = client.post(
                "/ai/image-decision",
                json={"context": {"selected_word": "octopus", "current_paragraph": "It swam."}},
            )
        assert response.status_code == 200
        assert response.json()["data"]["data"]["image_type"] == "photo"

    def test_novel_mode_route(self, client):
        payload = {
            "scene_mood": "suspense",
            "emotion": "tension",
            "intensity": 0.7,
            "companion_commentary": "Something lurks.",
        }
        with patch(
            "backend.app.modules.ai_engine.engines._get_client",
            return_value=mock_groq(payload),
        ):
            response = client.post(
                "/ai/novel-mode", json={"text": "A shadow moved in the dark."}
            )
        assert response.status_code == 200
        data = response.json()["data"]["data"]
        assert data["scene_mood"] == "suspense"
        assert data["intensity"] == 0.7
