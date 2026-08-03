import base64
from difflib import SequenceMatcher
import os
import sys
import cv2
from groq import Groq
import numpy as np
import requests

# Windows consoles default to cp1252 and cannot encode the emoji in the status
# output below; force UTF-8 so a log line never aborts frame processing.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# ================= CONFIGURATION =================
# Secrets are read from the environment (.env at project root). Never hardcode keys.
GOOGLE_VISION_API_KEY = os.environ.get("GOOGLE_VISION_API_KEY", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_FAST_MODEL = os.environ.get("GROQ_FAST_MODEL", "llama-3.1-8b-instant")
# =================================================

groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None


# ================= STEP 1: OPENCV PREPROCESSING =================
def preprocess_image(image_bytes):
    """Applies brightness, contrast enhancement (CLAHE), and sharpening to camera frames."""
    np_arr = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)

    if img is None:
        return image_bytes

    # Grayscale conversion
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # Contrast Enhancement
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)

    # Sharpening Filter
    kernel = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]])
    sharpened = cv2.filter2D(enhanced, -1, kernel)

    # Encode back to JPEG bytes
    _, encoded_img = cv2.imencode(".jpg", sharpened)
    return encoded_img.tobytes()


# ================= STEP 2: GOOGLE VISION OCR =================
def get_google_vision_text(image_bytes):
    """Executes DOCUMENT_TEXT_DETECTION on preprocessed frame."""
    if not GOOGLE_VISION_API_KEY:
        print("⚠️ GOOGLE_VISION_API_KEY not set — skipping OCR for this frame.")
        return ""

    image_base64 = base64.b64encode(image_bytes).decode("utf-8")
    vision_url = f"https://vision.googleapis.com/v1/images:annotate?key={GOOGLE_VISION_API_KEY}"

    payload = {
        "requests": [
            {
                "image": {"content": image_base64},
                "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
            }
        ]
    }

    response = requests.post(vision_url, json=payload, timeout=10)
    if response.status_code == 200:
        try:
            return response.json()["responses"][0]["fullTextAnnotation"]["text"]
        except (KeyError, IndexError):
            return ""
    return ""


# ================= STEP 3: GROQ MEMORY MERGE ENGINE =================
def merge_ocr_with_groq(current_merge_memory, new_ocr_text):
    """Groq reconstructs overlapping text fragments into clean page text."""
    system_prompt = """
You are the Memory Merge Engine for TaleTrace reading system.
Your SOLE responsibility is reconstructing and merging raw OCR fragments into a single, seamless, cleaned page text.

Rules:
1. Merge the 'New OCR Output' into the 'Current Merge Memory'.
2. Identify overlapping phrases, remove duplicate words, and fix broken sentences/paragraphs caused by camera movement.
3. Remove running headers (author name, book title "Septopus: Trouble on the High Cs") and standalone page numbers.
4. Smoothly repair missing OCR words using contextual language understanding.
5. DO NOT summarize, converse, or add introductory notes. Return ONLY the fully merged, properly formatted text.
"""

    user_prompt = f"""
Current Merge Memory:
{current_merge_memory if current_merge_memory else "[EMPTY - PAGE START]"}

New OCR Output:
{new_ocr_text}

Updated Merged Memory:
"""

    if groq_client is None:
        # Without a Groq key we cannot merge; append raw OCR so reading still works.
        print("⚠️ GROQ_API_KEY not set — skipping LLM merge, appending raw OCR text.")
        return (current_merge_memory + "\n" + new_ocr_text).strip()

    completion = groq_client.chat.completions.create(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        model=GROQ_MODEL,
        temperature=0.1,  # Low temperature for structural text precision
    )

    return completion.choices[0].message.content.strip()


# ================= STEP 4: PYTHON MEMORY MANAGER =================
class TaleTraceMemoryManager:

    def __init__(self):
        self.merge_memory = ""  # Active page state (Single source of truth)
        self.permanent_memory = []  # Archived completed pages
        self.current_reading_pointer = 0

    def check_same_page_via_groq(self, active_mem: str, raw_ocr: str) -> bool:
        """Asks Groq fast model whether the raw OCR frame belongs to the current page.

        Prevents false 'Page Transitions' due to slight camera movements or minor text differences.
        """
        if not active_mem.strip():
            return True

        if groq_client is None:
            # No key: assume same page rather than firing spurious page transitions.
            return True

        prompt = f"""Analyze these two text blocks from a reading camera stream:

EXISTING ACTIVE PAGE MEMORY:
"{active_mem[:400]}"

NEW OCR FRAME CAPTURE:
"{raw_ocr[:400]}"

Are both texts reading from the SAME page or chapter? (Even if words are slightly reordered, missing, or overlapping).
Answer with strictly ONE word: YES or NO."""

        try:
            response = groq_client.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                model=GROQ_FAST_MODEL,  # Ultra-fast execution (~150ms)
                temperature=0.0,
                max_tokens=5,
            )
            answer = response.choices[0].message.content.strip().upper()
            return "YES" in answer
        except Exception as e:
            print(f"⚠️ Groq similarity check error: {e}. Defaulting to same page.")
            return True

    def calculate_accurate_pointer(
        self, old_text: str, updated_merged_text: str
    ) -> int:
        """Accurately calculates where new text starts or was inserted in updated_merged_text."""
        if not old_text:
            return 0

        matcher = SequenceMatcher(None, old_text, updated_merged_text)
        match = matcher.find_longest_match(
            0, len(old_text), 0, len(updated_merged_text)
        )

        # Insertion offset is set to where the longest matching block ends in the merged text
        return match.b + match.size

    def process_frame(self, raw_image_bytes):
        """Processes a single camera frame through the strict 4-step pipeline."""

        # --- STEP 1: OpenCV Preprocessing ---
        processed_bytes = preprocess_image(raw_image_bytes)

        # --- STEP 2: Google Vision OCR ---
        latest_ocr = get_google_vision_text(processed_bytes)
        if not latest_ocr.strip():
            print("📷 Frame ignored: No text detected.")
            return self.merge_memory

        # --- STEP 4a: Fast Semantic Page Similarity Check ---
        is_same_page = True
        if self.merge_memory:
            is_same_page = self.check_same_page_via_groq(
                self.merge_memory, latest_ocr
            )

        if not is_same_page:
            print("\n🔄 PAGE TRANSITION DETECTED! (Different page content)")
            self.commit_to_permanent_memory()
            self.current_reading_pointer = 0  # Reset audio/TTS pointer

        # --- STEP 3: Groq Memory Merge Engine ---
        print("🧠 Reconstructing & merging text via Groq...")
        reconstructed_text = merge_ocr_with_groq(self.merge_memory, latest_ocr)

        # --- STEP 4b: Pointer Alignment & Memory State Update ---
        if is_same_page and self.merge_memory:
            self.current_reading_pointer = self.calculate_accurate_pointer(
                self.merge_memory, reconstructed_text
            )

        self.merge_memory = reconstructed_text

        print("\n✅ UPDATED MERGE MEMORY (Active Single Source of Truth):")
        print("-------------------------------------------------------")
        print(self.merge_memory)
        print("-------------------------------------------------------\n")

        self.sync_reading_pointer()

        return self.merge_memory

    def sync_reading_pointer(self):
        """Locks active sentence reading index during dynamic same-page updates."""
        print(
            f"📍 Reading Pointer synchronized. Locked at offset index: {self.current_reading_pointer}"
        )

    def commit_to_permanent_memory(self):
        """Commits active page to long-term memory and clears temporary state."""
        if self.merge_memory.strip():
            self.permanent_memory.append(self.merge_memory)
            print(
                f"💾 Page committed to Permanent Memory. Total Pages Saved: {len(self.permanent_memory)}"
            )
        self.merge_memory = ""

    def shutdown_session(self):
        """Commits active page memory when camera/reading session turns off."""
        print("\n🔌 Camera/Session end signal received.")
        self.commit_to_permanent_memory()
        print("🎉 Reading session finalized successfully.")


# Export global pipeline manager
pipeline = TaleTraceMemoryManager()