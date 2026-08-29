"""Synthetic Page Generation & Memory Merge End-to-End Validation.

1. Generates high-fidelity synthetic book page images and OCR response fixtures.
2. Simulates realistic camera captures with overlapping frame fragments.
3. Validates Merge Memory:
   - Frame accumulation and same-page overlap detection.
   - Page turn detection and immutable page history commits.
   - Version incrementing and ContentMap synchronization.
4. Validates downstream Reading Engine, Reading Speed, and Focus Analytics.
"""

import sys
import os
import json
import time
import asyncio
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import cv2
import numpy as np

from scripts.synthetic_page import render_page, PAGES, PAGE_WIDTH, PAGE_HEIGHT
from backend.app.modules.merge_memory.engine import MergeMemory
from backend.app.modules.ocr.pipeline import OcrPipeline, SAME_PAGE_OVERLAP_RATIO
from backend.app.modules.ocr.models import RecognizedWord
from backend.app.modules.reading_speed.models import ReadingPointer, ReadingBaseline
from backend.app.modules.reading_engine.engine import ReadingEngine
from backend.app.modules.reading_speed.service import ReadingSpeedService
from backend.app.modules.focus_analytics import FocusAnalyticsEngine
from backend.app.modules.preprocessing.text_normalizer import normalize_reading_text

async def test_synthetic_generation_and_merge():
    print("=" * 80)
    print("   SYNTHETIC PAGE GENERATION & MERGE MEMORY PIPELINE TEST")
    print("=" * 80)
    
    out_dir = REPO_ROOT / "tests" / "synthetic_corpus"
    out_dir.mkdir(parents=True, exist_ok=True)
    
    print("\n[Step 1] Rendering 4 synthetic book pages...")
    rendered_pages = []
    
    for idx, page_data in enumerate(PAGES, 1):
        img, grouped_words = render_page(page_data)
        img_path = out_dir / f"synthetic_page_{idx:02d}.jpg"
        cv2.imwrite(str(img_path), img)
        
        # Flatten words
        flat_words = [w for p in grouped_words for w in p]
        
        # Convert to RecognizedWord models
        rec_words = [
            RecognizedWord.from_bbox(
                text=w["text"],
                bbox=w["box"],
                confidence=1.0,
                word_index=w_idx
            )
            for w_idx, w in enumerate(flat_words)
        ]
        
        rendered_pages.append({
            "page_num": idx,
            "img_path": img_path,
            "paragraphs": page_data,
            "rec_words": rec_words,
            "total_words": len(rec_words)
        })
        
        print(f"  Page {idx}: {len(page_data)} paragraphs, {len(rec_words)} words rendered -> {img_path.name}")
    
    # -------------------------------------------------------------
    # Step 2: Merge Memory Frame Accumulation & Overlap Test
    # -------------------------------------------------------------
    print("\n" + "-" * 80)
    print("   [Step 2] Testing Merge Memory Multi-Frame Accumulation (Page 1)")
    print("-" * 80)
    
    memory = MergeMemory()
    p1 = rendered_pages[0]
    
    # Split Page 1 into two overlapping camera views
    half_idx = len(p1["rec_words"]) // 2
    # Frame 1A: First 60% of words
    frame_1a_words = p1["rec_words"][:half_idx + 5]
    frame_1a_text = " ".join(w.text for w in frame_1a_words)
    
    # Frame 1B: Overlapping second half (from 40% to end)
    frame_1b_words = p1["rec_words"][half_idx - 5:]
    frame_1b_text = " ".join(w.text for w in frame_1b_words)
    
    print(f">> Frame 1A (Top of Page 1): {len(frame_1a_words)} words")
    v1 = memory.apply_frame(frame_1a_text, page_index=1)
    print(f"   Memory Version after Frame 1A: {v1} (Held words: {memory.total_words})")
    assert v1 == 1
    assert memory.page_count == 1
    
    print(f">> Frame 1B (Bottom of Page 1 with overlap): {len(frame_1b_words)} words")
    v2 = memory.apply_frame(frame_1b_text, page_index=1)
    print(f"   Memory Version after Frame 1B: {v2} (Held words: {memory.total_words})")
    assert v2 == 2
    assert memory.page_count == 1  # Still on Page 1 (no spurious page turn)
    
    # -------------------------------------------------------------
    # Step 3: Page Turn & History Commitment Test
    # -------------------------------------------------------------
    print("\n" + "-" * 80)
    print("   [Step 3] Testing Page Turn & History Immutability")
    print("-" * 80)
    
    # Reader turns to Page 2
    p2 = rendered_pages[1]
    p2_text = " ".join(w.text for w in p2["rec_words"])
    
    print(f">> Moving to Page 2 ({len(p2['rec_words'])} words)...")
    memory.begin_page(page_index=2)
    v3 = memory.apply_frame(p2_text, page_index=2)
    
    print(f"   Memory Version after Page 2: {v3} (Total pages known: {memory.page_count})")
    assert memory.current_page == 2
    assert memory.page_count == 2
    
    # Check that Page 1 is now marked committed
    committed = memory.committed_pages()
    print(f"   Committed pages in history: {len(committed)} (Page {committed[0].page_index})")
    assert len(committed) == 1
    assert committed[0].page_index == 1
    assert committed[0].committed is True
    
    # Reader turns to Page 3 & Page 4
    for p_idx in [2, 3]:
        p = rendered_pages[p_idx]
        p_text = " ".join(w.text for w in p["rec_words"])
        memory.begin_page(page_index=p["page_num"])
        memory.apply_frame(p_text, page_index=p["page_num"])
        print(f"   Applied Page {p['page_num']}: {p['total_words']} words -> Version {memory.version}")
        
    memory.commit_page(page_index=4)
    print(f"\nFinal Memory State:")
    print(f"  Total Pages Known: {memory.page_count}")
    print(f"  Committed Pages: {len(memory.committed_pages())}")
    print(f"  Total Words in Session: {memory.total_words}")
    assert memory.page_count == 4
    assert len(memory.committed_pages()) == 4
    
    # -------------------------------------------------------------
    # Step 4: Content Map & Downstream Engine Integration
    # -------------------------------------------------------------
    print("\n" + "-" * 80)
    print("   [Step 4] Content Map & Reading Engine Progress Tracking")
    print("-" * 80)
    
    cmap = memory.content_map(source_version=memory.version)
    print(f"  ContentMap generated with {len(cmap.sentences)} total sentence spans across 4 pages.")
    assert len(cmap.sentences) > 0
    assert len(cmap.page_word_counts) == 4
    
    speed_service = ReadingSpeedService()
    speed_service.restore_baseline(
        ReadingBaseline(reader_id="synthetic-reader", baseline_wpm=220.0)
    )
    focus_engine = FocusAnalyticsEngine(
        session_id="synthetic-merge-test",
        reader_id="synthetic-reader",
    )
    
    reading_engine = ReadingEngine(
        session_id="synthetic-merge-test",
        reader_id="synthetic-reader",
        memory=memory,
        speed=speed_service,
        focus=focus_engine,
    )
    
    # Start session
    await reading_engine.start_session()
    print("  Reading session started successfully.")
    
    # Advance pointer across sentence spans
    for s_idx, span in enumerate(cmap.sentences[:6]):
        await reading_engine.move_pointer(span.pointer)
        
    print(f"  Reading Progress: Pointer at Page {reading_engine.state.pointer.page_index}, paragraph {reading_engine.state.pointer.paragraph_index}, sentence {reading_engine.state.pointer.sentence_index}.")
    
    # Finish session
    analytics = await reading_engine.finish_session(review=False)
    print(f"  Session Finished:")
    print(f"    Pages Read: {analytics.pages_read}")
    print(f"    Words Read: {analytics.words_read}")
    print(f"    Session Pace: {analytics.session_wpm:.1f} WPM")
    print(f"    Focus Report Present: {reading_engine.focus_report is not None}")
    
    assert analytics.words_read > 0
    
    print("\n" + "=" * 80)
    print("   ALL SYNTHETIC PIPELINE & MERGE MEMORY TESTS PASSED (100% SUCCESS)!")
    print("=" * 80)

if __name__ == "__main__":
    asyncio.run(test_synthetic_generation_and_merge())
