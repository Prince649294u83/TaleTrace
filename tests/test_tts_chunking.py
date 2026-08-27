"""Tests for the TTS chunking logic."""

from __future__ import annotations

import pytest

from backend.app.modules.audio_engine.sentence_queue import _chunk_for_tts, segment_sentences


class TestTTSChunking:
    def test_short_sentence_is_not_chunked(self):
        sentence = "This is a short sentence."
        chunks = _chunk_for_tts(sentence, limit=200)
        
        assert len(chunks) == 1
        assert chunks[0][0] == sentence
        assert chunks[0][1] == 0

    def test_chunking_on_comma(self):
        # 42 characters total. We'll set limit to 25.
        sentence = "This is a long sentence, which has a comma."
        chunks = _chunk_for_tts(sentence, limit=25)
        
        assert len(chunks) == 2
        assert chunks[0][0] == "This is a long sentence,"
        assert chunks[0][1] == 0
        
        assert chunks[1][0] == "which has a comma."
        assert chunks[1][1] == 25  # 'w' is at index 25

    def test_chunking_on_space_if_no_punctuation(self):
        sentence = "This is a long sentence without any punctuation marks in it."
        chunks = _chunk_for_tts(sentence, limit=25)
        
        assert len(chunks) == 3
        # First chunk: up to limit 25. "This is a long sentence w" - space is at 23.
        assert chunks[0][0] == "This is a long sentence"
        assert chunks[0][1] == 0
        
        # Next chunk from index 24.
        assert chunks[1][0] == "without any punctuation"
        assert chunks[1][1] == 24
        
        # Last chunk.
        assert chunks[2][0] == "marks in it."
        assert chunks[2][1] == 48

    def test_exact_limit_no_whitespace(self):
        sentence = "A" * 50
        chunks = _chunk_for_tts(sentence, limit=20)
        
        assert len(chunks) == 3
        assert chunks[0][0] == "A" * 20
        assert chunks[0][1] == 0
        assert chunks[1][0] == "A" * 20
        assert chunks[1][1] == 20
        assert chunks[2][0] == "A" * 10
        assert chunks[2][1] == 40

    def test_trailing_whitespace_stripped(self):
        sentence = "Hello world  "
        chunks = _chunk_for_tts(sentence, limit=5)
        # "Hello" (len 5)
        # "world" (len 5)
        
        assert len(chunks) == 2
        assert chunks[0][0] == "Hello"
        assert chunks[1][0] == "world"

    def test_segment_sentences_integrates_chunking(self):
        text = "Short sentence. Long sentence that exceeds the small limit set for testing."
        # Use a very small limit just for testing segment_sentences if we monkeypatch,
        # but _TTS_CHAR_LIMIT is 200. We can just test a >200 char string.
        
        long_text = "A" * 150 + " " + "B" * 150
        chunks = segment_sentences(long_text)
        
        assert len(chunks) == 2
        assert chunks[0].text == "A" * 150
        assert chunks[0].pointer.character_offset == 0
        assert chunks[0].pointer.sentence_index == 0
        
        assert chunks[1].text == "B" * 150
        assert chunks[1].pointer.character_offset == 151
        assert chunks[1].pointer.sentence_index == 0

    def test_empty_string(self):
        assert _chunk_for_tts("") == []
        assert _chunk_for_tts("   ") == []

    def test_all_invariants_met(self):
        sentence = "Alice said, 'Hello there!' and then she, being very tired, walked all the way back to her house in the country."
        limit = 30
        chunks = _chunk_for_tts(sentence, limit=limit)
        
        # Reconstruct to ensure no semantic loss
        for text, offset in chunks:
            assert len(text) <= limit
            assert text == sentence[offset:offset + len(text)]
            assert text.strip() == text
