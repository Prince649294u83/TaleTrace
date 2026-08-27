"""
Hardware Diagnostics Gate for TaleTrace.

Purely observational. Never modifies hardware, configuration, or code.
"""

import os
import sys
import socket
import urllib.request
import urllib.error
import urllib.parse
import json

# Ensure we can import backend
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def _print_header(title: str):
    print("=" * 50)
    print(f"        {title}")
    print("=" * 50)
    print()

def _check_software():
    print("Software")
    
    # Python version
    if sys.version_info >= (3, 12):
        print(f"[PASS] Python {sys.version_info.major}.{sys.version_info.minor} (venv)")
    else:
        print(f"[WARN] Python 3.12+ recommended, found {sys.version_info.major}.{sys.version_info.minor}")

    # Dependencies
    missing_deps = []
    for pkg in ["requests", "cv2", "numpy", "fastapi", "groq"]:
        try:
            __import__(pkg)
        except ImportError:
            missing_deps.append(pkg)
    if missing_deps:
        print(f"[FAIL] Missing dependencies: {', '.join(missing_deps)}")
    else:
        print("[PASS] Required dependencies (requests, cv2, numpy, fastapi, groq)")

    # .env
    if os.path.exists(".env"):
        print("[PASS] .env file exists")
    else:
        print("[FAIL] .env file missing")

    ocr_space = os.environ.get("OCR_SPACE_API_KEY")
    google_vision = os.environ.get("GOOGLE_VISION_API_KEY")
    if ocr_space and google_vision:
        print("[PASS] OCR configured: OCR.Space Engine 2 (primary), Vision (fallback)")
    elif ocr_space:
        print("[PASS] OCR configured: OCR.Space Engine 2")
    elif google_vision:
        print("[PASS] OCR configured: Google Vision (fallback)")
    else:
        print("[WARN] No OCR key configured -- set OCR_SPACE_API_KEY or GOOGLE_VISION_API_KEY")

    groq_1 = os.environ.get("GROQ_API_KEY_1")
    if groq_1:
        print("[PASS] GROQ_API_KEY_1 configured (AI Engine)")
    else:
        print("[WARN] GROQ_API_KEY_1 not configured")
        
    groq_2 = os.environ.get("GROQ_API_KEY_2")
    if groq_2:
        print("[PASS] GROQ_API_KEY_2 configured (Merge/Format)")
    else:
        print("[WARN] GROQ_API_KEY_2 not configured")

    groq_3 = os.environ.get("GROQ_API_KEY_3")
    if groq_3:
        print("[PASS] GROQ_API_KEY_3 configured (Learning Engine)")
    else:
        print("[WARN] GROQ_API_KEY_3 not configured — quiz/flashcards will gracefully degrade")

    from backend.app.shared.groq_keys import chat_model
    groq_model = chat_model()
    if groq_model:
        print(f"[PASS] GROQ_MODEL = {groq_model}")
    else:
        print("[WARN] GROQ_MODEL not configured")
    print()

def _check_source_integrity():
    print("Source Integrity")
    path = "backend/app/live_session.py"
    if not os.path.exists(path):
        print(f"[FAIL] {path} not found")
        print()
        return

    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    conflict_markers = ["<<<<<<< ", "======= ", ">>>>>>> "]
    has_conflicts = False
    for marker in conflict_markers:
        if marker in content:
            has_conflicts = True
            break
            
    if has_conflicts:
        print(f"[FAIL] Git conflict markers found in {path}")
    else:
        print(f"[PASS] No Git conflict markers (<<<<<<<, =======, >>>>>>>) in {path}")

    try:
        compile(content, path, "exec")
        print(f"[PASS] {path} syntax compiled successfully")
    except SyntaxError as e:
        print(f"[FAIL] Syntax error in {path}: {e}")
    print()

def _check_backend():
    print("Backend")
    
    port = 8000
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.0)
    
    try:
        # Check if port is in use
        result = s.connect_ex(("127.0.0.1", port))
        if result == 0:
            # Port is occupied, check if it's our backend answering
            try:
                import requests
                resp = requests.get(f"http://127.0.0.1:{port}/health", timeout=2.0)
                if resp.status_code == 200:
                    print(f"[PASS] BACKEND RESPONDING (port {port})")
                else:
                    print(f"[WARN] BACKEND NOT RESPONDING (port {port} returned {resp.status_code})")
            except requests.RequestException:
                print(f"[WARN] PORT OCCUPIED (port {port}) but BACKEND NOT RESPONDING to /health")
        else:
            print(f"[PASS] PORT AVAILABLE (port {port})")
    finally:
        s.close()
    print()

def _get_local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # doesn't even have to be reachable
        s.connect(('10.255.255.255', 1))
        IP = s.getsockname()[0]
    except Exception:
        IP = '127.0.0.1'
    finally:
        s.close()
    return IP

def _check_network():
    print("Network")
    local_ip = _get_local_ip()
    print(f"[PASS] Local machine: {local_ip}")
    
    cam_ip = ""
    cam_url = os.environ.get("ESP32_CAM_CAPTURE_URL")
    if cam_url:
        cam_ip = urllib.parse.urlparse(cam_url).hostname
    
    btn_ip = ""
    btn_url = os.environ.get("ESP32_BUTTONS_URL")
    if btn_url:
        btn_ip = urllib.parse.urlparse(btn_url).hostname
        
    local_subnet = ".".join(local_ip.split(".")[:3])
    
    compatible = True
    if cam_ip and ".".join(cam_ip.split(".")[:3]) != local_subnet:
        compatible = False
    if btn_ip and ".".join(btn_ip.split(".")[:3]) != local_subnet:
        compatible = False
        
    if compatible:
        print(f"[PASS] ESP32 devices on compatible subnet ({local_subnet}.x)")
    else:
        print(f"[WARN] ESP32 devices might be on different subnets than the local machine")
    print()

def _check_hardware():
    print("Hardware")
    
    import requests
    import time
    hardware_attached = False

    cam_url = os.environ.get("ESP32_CAM_CAPTURE_URL")
    if not cam_url:
        print("[WARN] ESP32-CAM: NOT CONFIGURED")
    else:
        cam_attached = False
        for attempt in range(5):
            try:
                resp = requests.get(cam_url, timeout=3.0)
                if resp.status_code == 200 and 'image/jpeg' in resp.headers.get('Content-Type', '') and len(resp.content) > 0:
                    print("[PASS] ESP32-CAM: ATTACHED")
                    hardware_attached = True
                    cam_attached = True
                    break
            except requests.RequestException:
                pass
            if attempt < 4:
                time.sleep(1.0)
                
        if not cam_attached:
            try:
                resp = requests.get(cam_url, timeout=3.0)
                if resp.status_code == 200 and 'image/jpeg' in resp.headers.get('Content-Type', '') and len(resp.content) > 0:
                    pass
                else:
                    print("[WARN] ESP32-CAM: UNREACHABLE / BAD CONTENT")
                    print(f"       (GET {cam_url} returned status {resp.status_code})")
            except requests.RequestException:
                print("[WARN] ESP32-CAM: NOT ATTACHED / UNREACHABLE")
                print(f"       (GET {cam_url} failed after retries)")
                print("       Meaning: device may be powered off, disconnected, or on another network.")

    btn_url = os.environ.get("ESP32_BUTTONS_URL")
    if not btn_url:
        print("[WARN] ESP32 buttons: NOT CONFIGURED")
    else:
        btn_attached = False
        for attempt in range(5):
            try:
                resp = requests.get(btn_url, timeout=3.0)
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                        if isinstance(data, dict):
                            print("[PASS] ESP32 buttons: ATTACHED")
                            hardware_attached = True
                            btn_attached = True
                            break
                    except ValueError:
                        pass
            except requests.RequestException:
                pass
            if attempt < 4:
                time.sleep(1.0)
                
        if not btn_attached:
            try:
                resp = requests.get(btn_url, timeout=3.0)
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                        if not isinstance(data, dict):
                            print("[WARN] ESP32 buttons: INVALID JSON SHAPE")
                            print(f"       (GET {btn_url} returned {type(data)})")
                    except ValueError:
                        print("[WARN] ESP32 buttons: INVALID JSON")
                else:
                    print("[WARN] ESP32 buttons: UNREACHABLE / BAD STATUS")
                    print(f"       (GET {btn_url} returned status {resp.status_code})")
            except requests.RequestException:
                print("[WARN] ESP32 buttons: NOT ATTACHED / UNREACHABLE")
                print(f"       (GET {btn_url} failed after retries)")

    print("=" * 50)
    if hardware_attached:
        print("Hardware check completed. Devices detected.")
    else:
        print("Hardware is not currently attached.")
    print("=" * 50)

if __name__ == "__main__":
    from backend.app.core.environment import load_environment
    load_environment()
    _print_header("TaleTrace Hardware Diagnostics")
    _check_software()
    _check_source_integrity()
    _check_backend()
    _check_network()
    _check_hardware()
