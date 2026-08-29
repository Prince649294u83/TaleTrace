# TaleTrace Hardware Testing & Rehearsal Master Guide

This guide is a complete, step-by-step, zero-prerequisite manual for setting up, wiring, calibrating, and testing the **TaleTrace physical smart bookmark rig**.

Whether you are starting from a completely blank machine or preparing for a live demonstration rehearsal, follow each section in exact numerical order.

---

## Table of Contents

1. [Hardware & Software Prerequisites](#1-hardware--software-prerequisites)
2. [Step 1: Clone Repository & Create Virtual Environment](#step-1-clone-repository--create-virtual-environment)
3. [Step 2: Install Python & Frontend Dependencies](#step-2-install-python--frontend-dependencies)
4. [Step 3: Hardware Wiring & Pinout Guide](#step-3-hardware-wiring--pinout-guide)
5. [Step 4: Flash Firmware to ESP32-CAM and ESP32 DevKit](#step-4-flash-firmware-to-esp32-cam-and-esp32-devkit)
6. [Step 5: Configure Environment Variables (.env)](#step-5-configure-environment-variables-env)
7. [Step 6: Direct Hardware Endpoint Verification (By Hand)](#step-6-direct-hardware-endpoint-verification-by-hand)
8. [Step 7: Run Automated Preflight & Diagnostics](#step-7-run-automated-preflight--diagnostics)
9. [Step 8: Camera Preview & Physical Framing Calibration](#step-8-camera-preview--physical-framing-calibration)
10. [Step 9: Run Automated Test Suites](#step-9-run-automated-test-suites)
11. [Step 10: Live Physical Reading Session & Rehearsal Flows](#step-10-live-physical-reading-session--rehearsal-flows)
12. [Step 11: Companion Web Application Verification](#step-11-companion-web-application-verification)
13. [Step 12: Offline Dataset Recording & Replay Tooling](#step-12-offline-dataset-recording--replay-tooling)
14. [Step 13: Hardware Troubleshooting Matrix](#step-13-hardware-troubleshooting-matrix)

---

## 1. Hardware & Software Prerequisites

### Required Software on Host PC
- **Operating System**: Windows 10/11, macOS, or Linux.
- **Python**: **Python 3.12** is required (MediaPipe does not support Python 3.13+).
- **Node.js**: Node.js **v20+** and npm (for the companion website).
- **Arduino IDE**: Version 2.x+ (with ESP32 board support and `U8g2` library installed).
- **Git**: For source control.

### Required Physical Components
1. **ESP32-CAM** (AI Thinker module with OV2640 camera sensor) + FTDI USB programmer.
2. **ESP32 DevKit Board** (30-pin or 38-pin ESP-WROOM-32).
3. **128×64 I2C OLED Display** (SH1106 or SSD1306 controller).
4. **1 × Momentary Push Button** (Normally Open).
5. **1 × Latching Toggle Switch** (SPDT / ON-OFF).
6. **Breadboard & Jumper Wires**.
7. **2.4 GHz Wi-Fi Network** (Host PC and ESP32 boards must be on the same local subnet).

---

## Step 1: Clone Repository & Create Virtual Environment

Open your terminal (PowerShell or Git Bash on Windows, Terminal on Mac/Linux) and navigate to your project directory.

```bash
# 1. Clone the repository (or pull latest changes)
git clone https://github.com/Prince649294u83/TaleTrace.git
cd TaleTrace

# 2. Checkout the test/working branch
git checkout Latest-changes-test

# 3. Create a dedicated Python 3.12 virtual environment
python -m venv .venv
```

**Why**: Isolates project dependencies from your global Python environment and avoids package version conflicts.

---

## Step 2: Install Python & Frontend Dependencies

Activate your newly created virtual environment and install all backend and frontend packages.

### On Windows (PowerShell):
```powershell
# Activate virtual environment
.venv\Scripts\Activate.ps1

# Upgrade pip
python -m pip install --upgrade pip

# Install backend dependencies
pip install -r requirements.txt

# Install frontend dependencies
cd frontend
npm install
cd ..
```

### On Windows (Command Prompt):
```cmd
.venv\Scripts\activate.bat
pip install -r requirements.txt
cd frontend && npm install && cd ..
```

### On Linux / macOS:
```bash
source .venv/bin/activate
pip install -r requirements.txt
cd frontend && npm install && cd ..
```

**Why**: Installs OpenCV, MediaPipe, FastAPI, Edge-TTS, SQLAlchemy, Pytest, React, and Vite packages.

---

## Step 3: Hardware Wiring & Pinout Guide

Connect the components to the **ESP32 DevKit** board as shown below:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          ESP32 DEVKIT PINOUT WIRING                         │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│   [ ESP32 DevKit ]                                                          │
│     • GPIO 4  ──────► Momentary Push Button (Terminal 1)                   │
│     • GPIO 5  ──────► Toggle Switch (Common Pin / Center)                   │
│     • GPIO 21 ──────► SH1106 OLED SDA Pin                                  │
│     • GPIO 22 ──────► SH1106 OLED SCL Pin                                  │
│     • 3.3V    ──────► SH1106 OLED VCC Pin                                  │
│     • GND     ──────► Breadboard GND Rail                                  │
│                                                                             │
│   [ Breadboard GND Rail ]                                                   │
│     • Connects to Momentary Push Button (Terminal 2)                        │
│     • Connects to Toggle Switch (ON Pin)                                    │
│     • Connects to SH1106 OLED GND Pin                                       │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Wiring Summary Table

| Peripheral | ESP32 DevKit Pin | Other Connection | Description |
|---|---|---|---|
| **Momentary Button** | `GPIO 4` | `GND` | Active **LOW** (`INPUT_PULLUP`). Re-reads pointed line. |
| **Toggle Switch** | `GPIO 5` | `GND` | Active **LOW** (`INPUT_PULLUP`). Enters Meaning Mode. |
| **OLED SDA** | `GPIO 21` | `OLED SDA` | I2C Data Line for 128×64 SH1106 display. |
| **OLED SCL** | `GPIO 22` | `OLED SCL` | I2C Clock Line for 128×64 SH1106 display. |
| **OLED VCC** | `3.3V` or `VIN` | `OLED VCC` | 3.3V Power Supply. |
| **OLED GND** | `GND` | `OLED GND` | Common Ground. |

---

## Step 4: Flash Firmware to ESP32-CAM and ESP32 DevKit

### 4.1 Flashing the ESP32 DevKit (Buttons & OLED)
1. Open the **Arduino IDE**.
2. Go to **Sketch $\to$ Include Library $\to$ Manage Libraries...** and search for **`U8g2`** (by olikraus). Install the latest version.
3. Open `buttons_and_oled.ino` from the repo root.
4. Set your 2.4GHz Wi-Fi credentials at the top of the file:
   ```cpp
   const char* ssid     = "YOUR_WIFI_SSID";
   const char* password = "YOUR_WIFI_PASSWORD";
   ```
5. Select Board: **`ESP32 Dev Module`** (or `DOIT ESP32 DEVKIT V1`), select your COM port.
6. Click **Upload**.
7. Open the **Serial Monitor** at **`115200`** baud and press the **EN / RST** button on the ESP32.
8. Look for the startup log:
   ```text
   [BOOT] TaleTrace Button & Display Controller
   [WIFI] Connected! IP: 192.168.1.26, RSSI: -52 dBm
   [SERVER] HTTP server started on port 8080
   ```
   *(Note down the IP address printed, e.g. `192.168.1.26`)*.

---

### 4.2 Flashing the ESP32-CAM
1. Connect the ESP32-CAM to your FTDI programmer:
   - FTDI `VCC (5V)` $\to$ ESP32-CAM `5V`
   - FTDI `GND` $\to$ ESP32-CAM `GND`
   - FTDI `TX` $\to$ ESP32-CAM `U0R` (RX)
   - FTDI `RX` $\to$ ESP32-CAM `U0T` (TX)
   - ESP32-CAM `GPIO 0` $\to$ `GND` (required during flashing only).
2. Open `backend/app/OCRandGESTURE/espcam/Almost_final.ino` in Arduino IDE.
3. Set your Wi-Fi credentials:
   ```cpp
   const char* ssid     = "YOUR_WIFI_SSID";
   const char* password = "YOUR_WIFI_PASSWORD";
   ```
4. Select Board: **`AI Thinker ESP32-CAM`**, Partition Scheme: **`Huge APP (3MB No OTA)`**.
5. Click **Upload**.
6. When done, disconnect `GPIO 0` from `GND`, open Serial Monitor at **`115200`** baud, and press the reset button.
7. Note down the assigned IP address (e.g., `192.168.1.200`).

---

## Step 5: Configure Environment Variables (.env)

Copy the example configuration file to `.env`:

```bash
# Windows PowerShell
copy .env.example .env

# Linux / macOS / Git Bash
cp .env.example .env
```

Open `.env` in your text editor and fill in your API keys and the IP addresses obtained in Step 4:

```env
# ── API Keys ──────────────────────────────────────────────────────────────────
# GROQ_API_KEY_1 is dedicated to AI Engine (Meaning Mode explanations)
GROQ_API_KEY_1="gsk_your_first_groq_key_here"

# GROQ_API_KEY_2 is dedicated to Text Reconstruction / Merge Memory
GROQ_API_KEY_2="gsk_your_second_groq_key_here"

# Google Cloud Vision API Key (or OCR_SPACE_API_KEY)
GOOGLE_VISION_API_KEY="your_google_cloud_vision_key_here"

# Optional: Freesound API Key for dynamic ambient background audio
FREESOUND_API_KEY=""

# ── Hardware Rig URLs ─────────────────────────────────────────────────────────
# ESP32 DevKit button server (Port 8080)
ESP32_BUTTONS_URL="http://192.168.1.26:8080/buttons"

# ESP32 DevKit OLED display server (Port 8080)
ESP32_DISPLAY_URL="http://192.168.1.26:8080/display"

# ESP32-CAM snapshot capture endpoint (Port 80)
ESP32_CAM_CAPTURE_URL="http://192.168.1.200/capture"

# ── Audio & Models ────────────────────────────────────────────────────────────
AUDIO_PROVIDER="edge"
GROQ_MODEL="openai/gpt-oss-120b"
GROQ_FAST_MODEL="openai/gpt-oss-20b"
```

---

## Step 6: Direct Hardware Endpoint Verification (By Hand)

Before running the TaleTrace engine, confirm that your host machine can directly reach both ESP32 devices over HTTP.

### Test 1: Check ESP32 DevKit Health Telemetry
```bash
curl http://192.168.1.26:8080/health
```
**Expected Response**:
```json
{"device":"button_oled","protocol_version":1,"firmware_version":"1.2.0","ip":"192.168.1.26","port":8080,"uptime_ms":24500,"wifi_rssi":-54,"oled":true}
```
*Proves*: Web server is up, Wi-Fi signal is strong, and OLED display initialized.

---

### Test 2: Check Raw Button States
```bash
curl http://192.168.1.26:8080/buttons
```
**Expected Response**:
```json
{"btn_momentary":false,"btn_toggle":false,"both_active":false}
```
*Now press and hold the momentary button or flip the toggle, and re-run the command to verify it reports `true`.*

---

### Test 3: Test Sending Text to the Physical OLED Screen
```bash
curl -X POST -H "Content-Type: text/plain" -d "TaleTrace OLED is Working!" http://192.168.1.26:8080/display
```
**Expected Response on Terminal**:
```text
Display updated (26 bytes, 1 pages)
```
**Expected on OLED Screen**:
`TaleTrace OLED is Working!` rendered cleanly in ProFont12 with automatic word wrapping.

---

### Test 4: Capture a Test Frame from ESP32-CAM
```bash
curl -o test_frame.jpg http://192.168.1.200/capture
```
**Expected Result**:
A valid JPEG photograph `test_frame.jpg` is saved to your current working directory.

---

## Step 7: Run Automated Preflight & Diagnostics

TaleTrace includes a comprehensive automated diagnostics suite that checks API keys, subnet compatibility, and device reachability.

```bash
# 1. Run the hardware diagnostics gate
python scripts/hardware_check.py
```

**Expected Output**:
```text
[✓] Python version: 3.12.x (Valid)
[✓] GROQ_API_KEY_1 is configured
[✓] GROQ_API_KEY_2 is configured
[✓] GOOGLE_VISION_API_KEY is configured
[✓] ESP32 Buttons reachable at http://192.168.1.26:8080/buttons
[✓] ESP32 OLED Display reachable at http://192.168.1.26:8080/display
[✓] ESP32-CAM reachable at http://192.168.1.200/capture
[✓] Hardware preflight PASSED: System ready for live session!
```

Next, run the live session preflight probe:
```bash
python -m backend.app.live_session --check
```

---

## Step 8: Camera Preview & Physical Framing Calibration

To ensure the camera sees the book page clearly and without perspective warp, use the **Live Camera Preview Tool**:

```bash
# 1. Start the backend server
python -m uvicorn backend.app.main:app --reload
```

2. Open the camera preview in your browser:
   - Double-click or open: `file:///E:/Projects/TaleTrace/scripts/camera_preview.html`
   - Or navigate to `http://127.0.0.1:8000/api/debug/camera` to view the live JPEG frame directly.
3. In `camera_preview.html`, click **Start Preview**.

### Camera Framing Guidelines
- **Page Fill**: Position the book so the printed text fills $\approx 80-90\%$ of the camera frame.
- **Lighting**: Diffused, uniform lighting. Avoid harsh shadows cast across the lines of text.
- **Orientation**: Ensure the page is right-side up relative to the camera sensor.
- **Focal Distance**: Ensure text is crisp and readable.

---

## Step 9: Run Automated Test Suites

Run the full automated test suite to ensure 100% of modules are verified:

```bash
# Run all backend tests (822 tests)
python -m pytest

# Run dedicated hardware integration tests (124 tests)
python -m pytest tests/test_device_integration.py tests/test_virtual_devices.py

# Run synthetic corpus generation and merge memory validation
python scripts/test_synthetic_pipeline.py

# Run frontend vitest tests (11 tests)
cd frontend && npm test && cd ..

# Run the end-to-end integration gate
python scripts/verify_all.py
```

**Expected Result**: All tests pass (`100% pass rate`).

---

## Step 10: Live Physical Reading Session & Rehearsal Flows

Now launch the real hardware reading loop:

```bash
python -m backend.app.live_session
```

### Golden Physical Interactions to Rehearse

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                              REHEARSAL EXECUTION CHECKLIST                             │
├────────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                        │
│  [A] Golden Path 1: Reading Pointer Advance                                            │
│      1. Ensure Toggle Switch is OFF.                                                   │
│      2. Place your index finger directly on a sentence on the printed page.           │
│      3. Press the Momentary Button (GPIO 4).                                           │
│      4. Observation: System captures frame burst → Fused detector identifies word →   │
│         Reading pointer updates → TTS voice narrates from pointed word aloud.          │
│                                                                                        │
│  [B] Golden Path 2: Meaning Mode Contextual Lookup                                     │
│      1. Flip Toggle Switch to ON (GPIO 5).                                             │
│      2. System emits MEANING_MODE_ON → Audio narration & background ambient PAUSE.     │
│      3. Point your index finger at a difficult or unfamiliar word.                     │
│      4. System captures burst → Resolves stable word → Queries AI Engine →             │
│         Displays contextual explanation on the physical OLED screen!                  │
│      5. Reading pointer does NOT move.                                                 │
│                                                                                        │
│  [C] Golden Path 3: Local OLED Explanation Pagination                                  │
│      1. While Toggle Switch is ON and explanation is visible on OLED.                  │
│      2. Press the Momentary Button (or press both buttons simultaneously).             │
│      3. ESP32 DevKit advances to Page 2 of multi-page explanation on the OLED.        │
│      4. Reading pointer does NOT advance; no backend server request is needed.         │
│                                                                                        │
│  [D] Golden Path 4: Exit Meaning Mode                                                  │
│      1. Flip Toggle Switch to OFF.                                                     │
│      2. System emits MEANING_MODE_OFF → OLED clears → Reading narration resumes.       │
│                                                                                        │
│  [E] End Session:                                                                      │
│      1. Press Ctrl+C in the terminal.                                                  │
│      2. Review the printed session summary (words read, pages, lookups, WPM).          │
│                                                                                        │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## Step 11: Companion Web Application Verification

TaleTrace includes a companion review website for inspecting session history, speed analytics, focus reports, and auto-generated quizzes and flashcards.

```bash
# Launch both backend and frontend servers simultaneously
python scripts/dev.py
```

1. Open your browser to the URL printed in the console (typically `http://localhost:5173`).
2. Log in with the demo reader account:
   - **Email**: `demo@taletrace.app`
   - **Password**: `demo1234`
3. Verify that your newly completed reading session from Step 10 appears on:
   - **Dashboard**: Live stats, words read, reading speed gauge.
   - **History**: Detailed session logs and difficulty ratings.
   - **Analysis**: The five analytical charts (Pace, Difficulty, Session Duration, etc.).
   - **Quizzes & Flashcards**: Generate review quizzes based on the words you looked up!

---

## Step 12: Offline Dataset Recording & Replay Tooling

For testing gesture stability, partial finger shapes, and lighting conditions offline without needing the physical rig active:

### Recording a Live Dataset
```bash
# Capture 100 synchronized frames + button states to recordings/session_01
python scripts/record_live_dataset.py --output recordings/session_01 --max-frames 100
```

### Replaying a Recorded Dataset
```bash
# Replay captured session through 3-tier gesture engine and calculate metrics
python scripts/replay_dataset.py --input recordings/session_01 --realtime
```

---

## Step 13: Hardware Troubleshooting Matrix

| Symptom | Probable Cause | Exact Fix |
|---|---|---|
| `[WinError 10013]` when starting backend | Port 8000 is occupied by a leftover backend process | Run: `netstat -ano \| findstr :8000`<br>Then: `taskkill /F /PID <PID>` |
| `Cannot start: no camera` | ESP32-CAM is powered down or IP address changed | Check power, confirm IP in serial monitor, test with `curl http://<cam-ip>/capture`. |
| `buttons: not reachable` | ESP32 DevKit is offline or on a different Wi-Fi subnet | Check 2.4GHz network, run `curl http://<devkit-ip>:8080/health`. |
| OLED screen is blank | Incorrect I2C wiring (SDA/SCL swapped or loose VCC) | Verify SDA $\to$ GPIO 21, SCL $\to$ GPIO 22, VCC $\to$ 3.3V, GND $\to$ GND. |
| MediaPipe fails to import | Running Python 3.13+ | Install Python 3.12 (`py -3.12 -m venv .venv`). |
| TTS audio does not play | `AUDIO_PROVIDER` is set incorrectly or network drop | Set `AUDIO_PROVIDER=edge` in `.env` (or `AUDIO_PROVIDER=offline` for OS voice). |
| Meaning Mode does not explain | `GROQ_API_KEY_1` is empty or invalid | Enter your valid Groq API key in `.env`. |
| Vite dev server starts on port 5174 | Port 5173 was busy | Normal behavior: open the exact `Local:` URL printed by Vite. |

---

**You are now completely ready to run a full physical hardware demonstration!** 🚀
