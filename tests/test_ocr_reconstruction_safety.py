"""Unit tests for safe dual-path OCR reconstruction, immutable geometry, and normalizer idempotence."""

import pytest
from backend.app.modules.ocr.models import RecognizedWord
from backend.app.modules.ocr.reconstruction_models import (
    RawOCRToken,
    NormalizedToken,
    DropCapCandidate,
    NormalizationRule,
    ReconstructionStatus,
)
from backend.app.modules.preprocessing.text_normalizer import (
    prune_leading_punctuation,
    normalize_numeric_decades,
    close_hyphenated_compounds,
    standardize_dashes,
    attach_punctuation_spacing,
    normalize_reading_text,
    detect_spatial_drop_cap,
    convert_recognized_words_to_raw_tokens,
)


def test_numeric_decade_positive_199os_to_1990s():
    text = "the glory of the late 199os can't imagine"
    cleaned, changed = normalize_numeric_decades(text)
    assert changed is True
    assert cleaned == "the glory of the late 1990s can't imagine"


def test_numeric_decade_negative_preserves_valid_words():
    # '100o' (not ending in s), 'boss', 'chaos' must NOT change
    text = "the score was 100o and the boss created chaos in the 1980s"
    cleaned, changed = normalize_numeric_decades(text)
    assert cleaned == "the score was 100o and the boss created chaos in the 1980s"


def test_hyphen_closure_positive_second_hand():
    text = "what you learn second - hand is useful"
    cleaned, changed = close_hyphenated_compounds(text)
    assert changed is True
    assert cleaned == "what you learn second-hand is useful"


def test_hyphen_negative_preserves_clause_dashes():
    text = "Chapter 1 - Introduction to Finance"
    cleaned, changed = close_hyphenated_compounds(text)
    # Digits around dash or full words with multi-spaces should not falsely collapse
    assert "Chapter 1 - Introduction" in cleaned


def test_leading_punctuation_pruning():
    text = ", values in different parts of the world"
    cleaned, changed = prune_leading_punctuation(text)
    assert changed is True
    assert cleaned == "values in different parts of the world"


def test_punctuation_attachment():
    text = "People do some crazy things with money . But no one is crazy ."
    cleaned, changed = attach_punctuation_spacing(text)
    assert changed is True
    assert cleaned == "People do some crazy things with money. But no one is crazy."


def test_dash_standardization():
    text = "So all of us - you , me , everyone - go through life"
    cleaned, changed = standardize_dashes(text)
    assert changed is True
    assert "—you, me, everyone—" in cleaned or "—" in cleaned


def test_normalizer_idempotence():
    corpus_samples = [
        "People do some crazy things with money . But no one is crazy .",
        ", values in different parts of the world , born into economies ,",
        "what you learn second - hand . So all of us — you , me , everyone - go",
        "the glory of the late 199os can't imagine .",
        "The Australian who hasn't seen a recession in 30 years has",
    ]
    for sample in corpus_samples:
        pass1 = normalize_reading_text(sample)
        pass2 = normalize_reading_text(pass1)
        assert pass1 == pass2, f"Idempotence failed on: {sample}"


def test_spatial_drop_cap_detection():
    raw_tokens = (
        RawOCRToken(
            token_id=0,
            text="ET",
            bbox=(260, 564, 311, 595),
            center_x=285.5,
            center_y=579.5,
            line_index=0,
        ),
        RawOCRToken(
            token_id=1,
            text="ME",
            bbox=(315, 565, 359, 595),
            center_x=337.0,
            center_y=580.0,
            line_index=0,
        ),
    )
    candidate = detect_spatial_drop_cap(raw_tokens)
    assert candidate is not None
    assert candidate.glyph == "L"
    assert candidate.reconstructed_text == "LET"
    assert candidate.target_raw_token_id == 0
    # Original raw token remains unchanged
    assert raw_tokens[0].text == "ET"
    assert raw_tokens[0].bbox == (260, 564, 311, 595)


def test_raw_ocr_tokens_are_frozen_immutable():
    token = RawOCRToken(
        token_id=1,
        text="challenge",
        bbox=(380, 580, 450, 610),
        center_x=415.0,
        center_y=595.0,
    )
    with pytest.raises(Exception):
        token.text = "MUTATED"  # type: ignore


def test_reconstruction_does_not_mutate_recognized_words():
    w = RecognizedWord.from_bbox("ET", (260, 564, 311, 595))
    tokens = convert_recognized_words_to_raw_tokens([w])
    assert len(tokens) == 1
    assert tokens[0].text == "ET"
    assert tokens[0].bbox == (260, 564, 311, 595)
    # The original RecognizedWord is also unaffected
    assert w.text == "ET"
    assert w.bbox == (260, 564, 311, 595)


class _FakeGroqClient:
    def __init__(self, responses=None, exception=None):
        self.responses = list(responses or [])
        self.exception = exception
        self.calls = []
        self.chat = type("Chat", (), {"completions": self})()

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.exception:
            exc = self.exception
            # If exception is a list, pop next
            if isinstance(exc, list):
                if exc:
                    curr = exc.pop(0)
                    if curr:
                        raise curr
            else:
                raise exc
        resp = self.responses.pop(0) if self.responses else "merged content"
        msg = type("Message", (), {"content": resp})()
        choice = type("Choice", (), {"message": msg})()
        return type("Completion", (), {"choices": [choice]})()


def test_groq_reconstructor_sends_max_tokens_candidate_850():
    from backend.app.modules.merge_memory.reconstruction import GroqReconstructor
    client = _FakeGroqClient(responses=["Reconstructed page text."])
    reconstructor = GroqReconstructor(client=client)

    result = reconstructor("Held memory text.", "New OCR line.")
    assert result == "Reconstructed page text."
    assert len(client.calls) == 1
    call_kwargs = client.calls[0]
    assert (
        call_kwargs.get("max_completion_tokens") == 850
        or call_kwargs.get("max_tokens") == 850
    ), "Must explicitly bound completion tokens to 850"


def test_groq_reconstructor_429_within_budget_retries_once():
    from backend.app.modules.merge_memory.reconstruction import GroqReconstructor
    exc_429 = RuntimeError("Rate limit reached. Please try again in 0.05s.")
    client = _FakeGroqClient(responses=["Successfully retried merge."], exception=[exc_429, None])
    reconstructor = GroqReconstructor(client=client)

    result = reconstructor("Held page text.", "Second sentence.")
    assert result == "Successfully retried merge."
    assert len(client.calls) == 2  # initial try + 1 retry


def test_groq_reconstructor_429_exceeding_budget_falls_back_immediately():
    from backend.app.modules.merge_memory.reconstruction import GroqReconstructor
    exc_long_429 = RuntimeError("Rate limit reached. Please try again in 15.0s.")
    client = _FakeGroqClient(exception=exc_long_429)
    reconstructor = GroqReconstructor(client=client)

    result = reconstructor("Held page text.", "Second sentence.")
    # Exceeds 1.5s MAX_MERGE_BLOCKING_BUDGET: falls back immediately to raw text
    assert result == "Held page text.\nSecond sentence."
    assert len(client.calls) == 1  # No sleep/retry attempted


def test_20_sequence_merge_stress_preserves_reading_continuity():
    """Verify 20-sequence merge stress test measuring 429s, retries, fallbacks, and text continuity."""
    from backend.app.modules.merge_memory.reconstruction import (
        GroqReconstructor,
        pointer_offset,
    )
    import time

    # Simulate realistic sequence: 14 normal merges, 3 transient 429s retried, 3 long 429s falling back
    exceptions = [
        None, None,
        RuntimeError("Rate limit reached. Please try again in 0.01s."), None,  # transient retry
        None, None,
        RuntimeError("Rate limit reached. Please try again in 10.0s."),  # long fallback
        None, None, None,
        RuntimeError("Rate limit reached. Please try again in 0.01s."), None,  # transient retry
        None, None,
        RuntimeError("Rate limit reached. Please try again in 10.0s."),  # long fallback
        None, None,
        RuntimeError("Rate limit reached. Please try again in 10.0s."),  # long fallback
        None, None,
    ]
    # Real LLM reconstructor returns cumulative merged text
    responses = [" ".join(f"Paragraph segment {j}." for j in range(i + 1)) for i in range(30)]
    client = _FakeGroqClient(responses=responses, exception=exceptions)
    reconstructor = GroqReconstructor(client=client)

    metrics = {
        "total_requests": 20,
        "latencies": [],
        "pointers": [],
    }

    current_mem = ""
    for i in range(20):
        t0 = time.perf_counter()
        new_ocr = f"Line {i} of book text."
        prev_mem = current_mem
        current_mem = reconstructor(current_mem, new_ocr)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        metrics["latencies"].append(elapsed_ms)

        # Calculate pointer offset for this merge
        offset = pointer_offset(prev_mem, current_mem)
        metrics["pointers"].append(offset)

        # Invariants: no text lost, monotonic text length progression
        assert len(current_mem) >= len(prev_mem), f"Text lost at merge step {i}"
        assert elapsed_ms < 1500.0, f"Merge step {i} exceeded 1.5s blocking budget: {elapsed_ms}ms"

    max_lat = max(metrics["latencies"])
    assert max_lat < 1500.0, f"Max merge latency {max_lat}ms exceeded 1.5s limit"
    assert len(metrics["pointers"]) == 20
    assert len(client.calls) >= 20  # initial calls + transient retries


def test_groq_reconstructor_client_config_max_retries_zero():
    """Verify GroqReconstructor configures the underlying SDK with max_retries=0."""
    from backend.app.modules.merge_memory.reconstruction import GroqReconstructor

    reconstructor = GroqReconstructor(api_key="gsk_test_fake_key")
    client = reconstructor._get_client()
    assert client is not None
    assert getattr(client, "max_retries", None) == 0


def test_groq_reconstructor_immediate_429_budget_enforcement():
    """Verify an HTTP 429 exception with 5s retry-after falls back immediately within budget."""
    import time
    from backend.app.modules.merge_memory.reconstruction import GroqReconstructor

    class _Immediate429Client:
        def __init__(self):
            self.chat = self
            self.completions = self

        def create(self, **kwargs):
            # Simulate Groq 429 RateLimitError
            raise RuntimeError("Rate limit reached. Please try again in 5.0s.")

    reconstructor = GroqReconstructor(client=_Immediate429Client())
    t0 = time.perf_counter()
    result = reconstructor("Held page text.", "New OCR text.")
    elapsed = time.perf_counter() - t0

    # Must fall back to appending raw OCR text in under 1.5s (should be < 50ms)
    assert elapsed < 0.1, f"Fallback took {elapsed}s, expected near-zero delay"
    assert "Held page text." in result
    assert "New OCR text." in result



