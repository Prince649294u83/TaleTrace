"""Test suite for AI Engine models, prompt building, and context resolution."""

import pytest

from backend.app.modules.ai_engine.models import (
    AiExplainRequest,
    AiInput,
    BookMetadata,
    DifficultyLevel,
    ImageType,
    LearnedWord,
    LookupRecord,
    ReadingContext,
    ReadingMode,
)
from backend.app.modules.ai_engine.prompts import PromptBuilder


class TestReadingContext:
    """Test the rich reading context model."""

    def test_default_construction(self):
        ctx = ReadingContext()
        assert ctx.mode == ReadingMode.STANDARD
        assert ctx.previously_explained == []
        assert ctx.book.title is None

    def test_full_context(self):
        ctx = ReadingContext(
            book=BookMetadata(title="Test Book", genre="Fiction", author="Author Name"),
            page_number=42,
            previous_paragraph="Previous text.",
            current_paragraph="Current text.",
            selected_word="test",
            mode=ReadingMode.ADAPTIVE,
            previously_explained=[LearnedWord(word="alpha", definition="first")],
        )
        assert ctx.book.title == "Test Book"
        assert ctx.page_number == 42
        assert ctx.selected_word == "test"
        assert ctx.mode == ReadingMode.ADAPTIVE
        assert len(ctx.previously_explained) == 1


class TestLearnedWord:
    """Test session memory word recap logic."""

    def test_recap_with_definition(self):
        word = LearnedWord(word="test", definition="a trial")
        assert word.recap() == "'test' meaning a trial"

    def test_recap_with_fun_definition_extra(self):
        word = LearnedWord(word="alpha", fun_definition="first thing")
        assert word.recap() == "'alpha' meaning first thing"

    def test_recap_no_meaning(self):
        word = LearnedWord(word="empty")
        assert word.recap() == "'empty'"


class TestAiInputResolution:
    """Test legacy flat field to ReadingContext resolution."""

    def test_new_style_context_preferred(self):
        request = AiInput(
            text="legacy paragraph",
            metadata={"word": "legacy"},
            context=ReadingContext(
                current_paragraph="new paragraph", selected_word="new"
            ),
        )
        ctx = request.resolved_context()
        assert ctx.current_paragraph == "new paragraph"
        assert ctx.selected_word == "new"

    def test_legacy_fields_fill_gaps(self):
        request = AiInput(text="The octopus swam.", metadata={"word": "octopus"})
        ctx = request.resolved_context()
        assert ctx.current_paragraph == "The octopus swam."
        assert ctx.selected_word == "octopus"

    def test_metadata_mode_resolution(self):
        request = AiInput(text="text", metadata={"mode": "adaptive"})
        ctx = request.resolved_context()
        assert ctx.mode == ReadingMode.ADAPTIVE

    def test_invalid_mode_defaults_to_standard(self):
        request = AiInput(text="text", metadata={"mode": "invalid_mode"})
        ctx = request.resolved_context()
        assert ctx.mode == ReadingMode.STANDARD

    def test_book_metadata_from_flat_fields(self):
        request = AiInput(
            text="text",
            metadata={
                "book_title": "Atomic Habits",
                "author": "James Clear",
                "genre": "Self Help",
                "chapter": "Ch 3",
            },
        )
        ctx = request.resolved_context()
        assert ctx.book.title == "Atomic Habits"
        assert ctx.book.author == "James Clear"
        assert ctx.book.genre == "Self Help"
        assert ctx.book.chapter == "Ch 3"


class TestAiExplainRequest:
    """Test the explain request's context merging."""

    def test_previously_explained_merges_into_context(self):
        request = AiExplainRequest(
            text="para",
            metadata={"word": "test"},
            previously_explained=[LearnedWord(word="alpha", definition="first")],
        )
        ctx = request.resolved_context()
        assert len(ctx.previously_explained) == 1
        assert ctx.previously_explained[0].word == "alpha"

    def test_context_previously_explained_takes_precedence(self):
        request = AiExplainRequest(
            text="para",
            metadata={"word": "test"},
            context=ReadingContext(
                previously_explained=[LearnedWord(word="beta", definition="second")]
            ),
            previously_explained=[LearnedWord(word="alpha", definition="first")],
        )
        ctx = request.resolved_context()
        assert len(ctx.previously_explained) == 1
        assert ctx.previously_explained[0].word == "beta"


class TestPromptBuilder:
    """Test prompt construction from reading context."""

    def test_book_description_all_fields(self):
        pb = PromptBuilder()
        ctx = ReadingContext(
            book=BookMetadata(
                title="Atomic Habits",
                author="James Clear",
                genre="Self Help",
                chapter="Ch 3",
                audience="adult",
            )
        )
        desc = pb.book_description(ctx)
        assert "Atomic Habits" in desc
        assert "James Clear" in desc
        assert "Self Help" in desc
        assert "Ch 3" in desc
        assert "adult" in desc

    def test_book_description_unknown_book(self):
        pb = PromptBuilder()
        ctx = ReadingContext()
        desc = pb.book_description(ctx)
        assert "unknown" in desc.lower()
        assert "infer" in desc.lower()

    def test_mode_guidance_for_all_modes(self):
        pb = PromptBuilder()
        for mode in ReadingMode:
            ctx = ReadingContext(mode=mode)
            guidance = pb.mode_guidance(ctx)
            assert len(guidance) > 0

    def test_session_memory_empty(self):
        pb = PromptBuilder()
        ctx = ReadingContext()
        memory = pb.session_memory(ctx)
        assert memory == ""

    def test_session_memory_with_words(self):
        pb = PromptBuilder()
        ctx = ReadingContext(
            previously_explained=[
                LearnedWord(word="alpha", definition="first"),
                LearnedWord(word="beta", definition="second"),
            ]
        )
        memory = pb.session_memory(ctx)
        assert "alpha" in memory
        assert "beta" in memory
        assert "connection" in memory.lower()

    def test_user_content_full_context(self):
        pb = PromptBuilder()
        ctx = ReadingContext(
            selected_word="portfolio",
            page_number=42,
            previous_paragraph="Prev para.",
            current_paragraph="Curr para.",
            mode=ReadingMode.STUDY,
        )
        content = pb.user_content(ctx)
        assert "portfolio" in content
        assert "42" in content
        assert "Prev para." in content
        assert "Curr para." in content
        assert "study" in content

    def test_explanation_prompt_contains_key_elements(self):
        pb = PromptBuilder()
        ctx = ReadingContext(
            book=BookMetadata(title="Test Book"),
            selected_word="test",
            mode=ReadingMode.ADAPTIVE,
        )
        prompt = pb.explanation_prompt(ctx)
        assert "TaleTrace" in prompt
        assert "oled_text" in prompt
        assert "full_explanation" in prompt
        assert "difficulty_level" in prompt
        assert "Test Book" in prompt

    def test_image_decision_prompt_contains_key_elements(self):
        pb = PromptBuilder()
        ctx = ReadingContext()
        prompt = pb.image_decision_prompt(ctx)
        assert "show_image" in prompt
        assert "image_query" in prompt
        assert "image_type" in prompt
        assert "reason" in prompt

    def test_novel_mode_prompt_includes_moods(self):
        pb = PromptBuilder()
        ctx = ReadingContext()
        moods = ["peaceful", "suspense", "action"]
        prompt = pb.novel_mode_prompt(ctx, moods)
        assert "scene_mood" in prompt
        assert "emotion" in prompt
        assert "intensity" in prompt
        assert "peaceful" in prompt
        assert "suspense" in prompt

    def test_summary_prompt_contains_key_elements(self):
        pb = PromptBuilder()
        ctx = ReadingContext(book=BookMetadata(title="Test Book"))
        prompt = pb.summary_prompt(ctx)
        assert "flashcards" in prompt
        assert "quiz" in prompt
        assert "words_learned" in prompt
        assert "session_summary" in prompt


class TestLookupRecord:
    """Test session history lookup records."""

    def test_lookup_record_construction(self):
        record = LookupRecord(
            word="test", context="The test was hard.", mode_used="adaptive", page_number=5
        )
        assert record.word == "test"
        assert record.context == "The test was hard."
        assert record.mode_used == "adaptive"
        assert record.page_number == 5

    def test_lookup_record_optional_fields(self):
        record = LookupRecord(word="test")
        assert record.context is None
        assert record.mode_used is None
        assert record.page_number is None


class TestEnums:
    """Test enum definitions."""

    def test_reading_mode_values(self):
        assert ReadingMode.STANDARD.value == "standard"
        assert ReadingMode.ADAPTIVE.value == "adaptive"
        assert ReadingMode.DISABILITY.value == "disability"
        assert ReadingMode.NOVEL.value == "novel"
        assert ReadingMode.STUDY.value == "study"
        assert ReadingMode.EXAM.value == "exam"

    def test_difficulty_level_values(self):
        assert DifficultyLevel.BEGINNER.value == "beginner"
        assert DifficultyLevel.INTERMEDIATE.value == "intermediate"
        assert DifficultyLevel.ADVANCED.value == "advanced"

    def test_image_type_values(self):
        assert ImageType.ILLUSTRATION.value == "illustration"
        assert ImageType.DIAGRAM.value == "diagram"
        assert ImageType.PHOTO.value == "photo"
        assert ImageType.MAP.value == "map"
        assert ImageType.PORTRAIT.value == "portrait"
        assert ImageType.NONE.value == "none"
