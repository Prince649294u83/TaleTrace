import os
import sys
import time
import requests
import numpy as np
import cv2

# ================= CONSOLE ENCODING =================
# Windows consoles default to cp1252, which cannot encode the emoji used in the
# status output below. Force UTF-8 so logging never crashes the control loop.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# ================= MODULE PATH SETUP =================
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(PROJECT_ROOT, "OCR_dynamicMem"))
sys.path.append(os.path.join(PROJECT_ROOT, "Gesture"))

# Load .env before importing modules that read configuration at import time.
try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
except ImportError:
    pass

from taletrace_processor import pipeline as ocr_pipeline
from gesture_engine import select_word
from ocr.google_vision_provider import GoogleVisionOCRProvider

# ================= CONFIGURATION =================
# Device endpoints and secrets are read from the environment (.env at project root).
ESP32_BUTTONS_URL = os.environ.get(
    "ESP32_BUTTONS_URL", "http://192.168.1.26:8080/buttons"
)  # ESP32 DevKit
ESP32_CAM_CAPTURE_URL = os.environ.get(
    "ESP32_CAM_CAPTURE_URL", "http://192.168.1.200/capture"
)  # ESP32-CAM
GOOGLE_VISION_API_KEY = os.environ.get("GOOGLE_VISION_API_KEY", "")
# ====================================================

if not GOOGLE_VISION_API_KEY:
    print(
        "⚠️ GOOGLE_VISION_API_KEY is not set. Copy .env.example to .env and add your key.",
        file=sys.stderr,
    )

http_session = requests.Session()

# Single reusable provider: converts raw JPEG bytes into List[OCRWord]
vision_provider = GoogleVisionOCRProvider(api_key=GOOGLE_VISION_API_KEY)


def poll_button_states():
    """Polls the ESP32 DevKit button server and returns (btn_momentary, btn_toggle)."""
    try:
        res = http_session.get(ESP32_BUTTONS_URL, timeout=0.3)
        if res.status_code == 200:
            data = res.json()
            return data.get("btn_momentary", False), data.get(
                "btn_toggle", False
            )
    except Exception:
        pass
    return False, False


def fetch_camera_frame():
    """Captures a single JPEG frame from the ESP32-CAM."""
    try:
        res = http_session.get(ESP32_CAM_CAPTURE_URL, timeout=1.5)
        if res.status_code == 200:
            return res.content
    except Exception:
        pass
    return None

def get_google_vision_words_direct(image_bytes):
    """Single-frame Google Vision OCR call for gesture word detection.

    Delegates to GoogleVisionOCRProvider so the Gesture Engine always receives
    proper OCRWord objects with (x_min, y_min, x_max, y_max) bounding boxes.
    """
    if not image_bytes:
        return []

    try:
        return vision_provider.extract(image_bytes)
    except Exception as e:
        print(f"⚠️ Direct Vision OCR error: {e}")
        return []


def run_gesture_selection():
    """Executes gesture word selection after a brief delay."""
    print("\n⏳ 1-second delay before gesture capture...")
    time.sleep(1.0)

    # 1. Fetch raw JPEG bytes from camera
    raw_bytes = fetch_camera_frame()
    if not raw_bytes:
        print("❌ Gesture failed: Could not capture frame from ESP32-CAM.")
        return None

    # 2. Decode raw bytes into a OpenCV NumPy Image Array (for Gesture Engine)
    np_arr = np.frombuffer(raw_bytes, np.uint8)
    image_frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)

    if image_frame is None:
        print("❌ Gesture failed: Could not decode image frame.")
        return None

    print("👆 Fetching frame word coordinates & executing Gesture Engine...")
    try:
        # 3. Get OCR word bounding boxes using raw_bytes
        frame_words = get_google_vision_words_direct(raw_bytes)

        # 4. Pass decoded NumPy image_frame AND frame_words to select_word
        result = select_word(
            image_frame,
            ocr_words=frame_words,
        )
        return result
    except Exception as e:
        print(f"❌ Error during Gesture Engine execution: {e}")
        return None

def print_gesture_details(result):
    if not result:
        print("⚠️ No target word selected.")
        return

    get_val = (
        (lambda k: result.get(k))
        if isinstance(result, dict)
        else (lambda k: getattr(result, k, "N/A"))
    )

    print(f"    Selected Word       : {get_val('selected_word')}")
    print(f"    Selected Line       : {get_val('selected_line')}")
    print(f"    Paragraph Context   : {get_val('selected_paragraph')}")
    print(f"    Selection Confidence: {get_val('confidence')}")


def main_loop():
    print("=" * 60)
    print("🚀 TaleTrace Main Controller Started")
    print("   - Default Mode: Continuous OCR_dynamicMem Reading")
    print("   - Button 1 (Momentary): Updated Reading (Word Select)")
    print("   - Button 2 (Latching): Meaning Mode (Holds execution)")
    print("=" * 60 + "\n")

    try:
        while True:
            # 1. ALWAYS check button status first
            btn_momentary, btn_toggle = poll_button_states()

            # -------------------------------------------------------------
            # CONDITION 1: Push-to-On Toggle Switch (Meaning Mode)
            # -------------------------------------------------------------
            if btn_toggle:
                print("\n📖 MEANING MODE ACTIVATED (Toggle Switch ON)")

                try:
                    gesture_result = run_gesture_selection()
                    if gesture_result:
                        print("\n💡 MEANING MODE RESULT:")
                        print_gesture_details(gesture_result)
                finally:
                    # Guarantees execution holds until toggle switch is physically turned OFF
                    print(
                        "\n🛑 Holding execution. Waiting for Toggle Switch to turn OFF..."
                    )
                    while True:
                        time.sleep(0.3)
                        _, current_toggle = poll_button_states()
                        if not current_toggle:
                            print(
                                "🔄 Toggle Switch turned OFF. Resuming OCR Loop...\n"
                            )
                            break

                continue

            # -------------------------------------------------------------
            # CONDITION 2: Momentary Push-to-On Button (Word Select)
            # -------------------------------------------------------------
            elif btn_momentary:
                print(
                    "\n🔘 MOMENTARY BUTTON PRESSED (Pausing OCR for Word Select)"
                )

                gesture_result = run_gesture_selection()
                if gesture_result:
                    print("\n📖 UPDATED READING RESULT:")
                    print_gesture_details(gesture_result)

                print(
                    "▶️ Resuming continuous OCR_dynamicMem reading loop...\n"
                )
                continue

            # -------------------------------------------------------------
            # CONDITION 3: Continuous OCR Processing
            # -------------------------------------------------------------
            else:
                frame_bytes = fetch_camera_frame()
                if frame_bytes:
                    ocr_pipeline.process_frame(frame_bytes)

            time.sleep(0.1)

    except KeyboardInterrupt:
        print("\n🛑 Manual interrupt received. Shutting down Session...")
    finally:
        ocr_pipeline.shutdown_session()


if __name__ == "__main__":
    main_loop()