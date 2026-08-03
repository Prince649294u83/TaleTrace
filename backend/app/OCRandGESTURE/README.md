# 📖 TaleTrace – Smart Bookmark & Reading Companion

**AI-Powered Context-Aware Physical Reading Companion**

TaleTrace is a hardware + software reading system for physical books. It combines real-time
computer vision, fingertip gesture recognition, dynamic OCR text merging, and LLM reasoning
to track reading progress and let you point at any word to pull its sentence and paragraph
context.

> All commands in this guide are for **Windows PowerShell**.

---

## 🌟 What It Does

- **Continuous reading** (`OCR_dynamicMem/`) — an ESP32-CAM streams page frames that are
  continuously merged into a single active page memory using Groq LLM inference, with automatic
  page-transition detection.
- **Point at a word** (`Gesture/`) — point at or touch any word to extract the target word, its
  surrounding sentence, and the full paragraph.
  - *Dual-tier detection*: MediaPipe hand landmarks, with an HSV skin-contour fallback.
  - *Line-first geometry*: direct-touch check first, then adaptive cone projection.
- **Two buttons, two modes**:
  - *Updated Reading* (momentary button) — pauses the stream and contextualizes the word under
    your finger.
  - *Meaning Mode* (latching toggle) — prints word/line/paragraph insight and holds until the
    switch is flipped back off.

---

## 📁 Repository Structure

```text
OCRandGESTURE/
├── main_controller.py            # Master orchestration controller — run this
├── test_buttons.py               # Hardware button test utility
├── requirements.txt              # Python dependencies
├── .env                          # Your API keys and device IPs (gitignored)
├── .env.example                  # Template for .env
│
├── esp32/
│   └── esp32.ino                 # ESP32 DevKit firmware (buttons, port 8080)
├── espcam/
│   └── Almost_final.ino          # ESP32-CAM firmware (JPEG /capture, port 80)
│
├── OCR_dynamicMem/
│   └── taletrace_processor.py    # OCR pipeline + Groq memory merge engine
│
└── Gesture/                      # Fingertip & word selection engine
    ├── gesture_engine/           # Detector, selector, models, visualizer
    ├── ocr/                      # JSON / PaddleOCR / Google Vision providers
    ├── tests/                    # Pytest suite (32 tests)
    └── main.py                   # Standalone gesture CLI
```

---

## 🛠️ Part 1 — Hardware Wiring

### ESP32 DevKit (the two buttons)

Uses the ESP32's internal pull-up resistors. Wire each switch directly between its GPIO pin and
GND — no external resistors needed.

| Component              | ESP32 Pin      | Reads LOW when   | What it does                      |
| ---------------------- | -------------- | ---------------- | --------------------------------- |
| Momentary push button  | GPIO 4         | pressed          | Word selection (Updated Reading)  |
| Latching toggle switch | GPIO 5         | switch is ON     | Meaning Mode (holds until OFF)    |
| Power                  | 5V / VIN + GND | —                | System power (5 V USB)            |

Serves `GET /buttons` on **port 8080** (default IP `192.168.1.26`).

### ESP32-CAM (the camera)

Serves one JPEG frame at `GET /capture` on **port 80** (default IP `192.168.1.200`).

### Flashing both boards

1. Open Arduino IDE.
2. Open `esp32\esp32.ino` and flash it to the DevKit.
3. Open `espcam\Almost_final.ino` and flash it to the ESP32-CAM.

> Both sketches have your WiFi name, WiFi password, and static IP hardcoded near the top.
> Update those lines before flashing if your network differs.

---

## 🚀 Part 2 — One-Time Software Setup

Open PowerShell in the project folder, then run these in order.

**Step 1 — Go to the project folder**

```powershell
cd e:\Projects\OCRandGESTURE\OCRandGESTURE
```

**Step 2 — Create the virtual environment**

```powershell
python -m venv .venv
```

**Step 3 — Activate it**

```powershell
.\.venv\Scripts\Activate.ps1
```

Your prompt should now start with `(.venv)`.

> If PowerShell blocks the script with an execution-policy error, allow it for this
> session only and then activate again:
>
> ```powershell
> Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
> .\.venv\Scripts\Activate.ps1
> ```

**Step 4 — Install dependencies**

```powershell
pip install -r requirements.txt
```

**Step 5 — Confirm it worked**

```powershell
python -m pytest Gesture\tests -q
```

You should see `32 passed`.

---

## ⚙️ Part 3 — Configuration

Your API keys and device addresses live in the `.env` file at the project root. This file is
already set up and is gitignored, so it never gets committed.

Every script reads its keys from `.env` automatically. **You never need to type or paste an API
key into a command.**

If you ever need to recreate `.env`, copy the template and fill in your own values:

```powershell
Copy-Item .env.example .env
notepad .env
```

```ini
GOOGLE_VISION_API_KEY=your_google_vision_key
GROQ_API_KEY=your_groq_key

ESP32_BUTTONS_URL=http://192.168.1.26:8080/buttons
ESP32_CAM_CAPTURE_URL=http://192.168.1.200/capture
```

Change the two URLs if your boards use different IP addresses.

---

## 💻 Part 4 — Running the System

Activate the environment first in every new PowerShell window:

```powershell
cd e:\Projects\OCRandGESTURE\OCRandGESTURE
.\.venv\Scripts\Activate.ps1
```

### Step 1 — Check the buttons work

Power on the DevKit, then run:

```powershell
python test_buttons.py
```

Press the button and flip the switch. You should see the states change live:

```text
Momentary (GPIO 4): 🔴 PRESSED     | Toggle (GPIO 5): 🟢 ON (Meaning)
```

Press `Ctrl+C` to stop.

> Seeing "Cannot connect to ESP32"? Check that the board is powered on, on the same WiFi, and
> that `ESP32_BUTTONS_URL` in `.env` matches its actual IP and uses port **8080**.

### Step 2 — Check the camera works

Open this in a browser, replacing the IP if yours differs:

```text
http://192.168.1.200/capture
```

You should get a single photo of whatever the camera sees. If so, the camera is ready.

### Step 3 — Start TaleTrace

```powershell
python main_controller.py
```

That's it. The system is now running. Press `Ctrl+C` to stop it, which also saves the current
page to permanent memory.

---

## 🎮 Part 5 — How To Use It

Point the camera at an open book page, then:

| What you do                        | What happens                                                      |
| ---------------------------------- | ----------------------------------------------------------------- |
| Nothing — just leave it running    | Continuously reads the page and rebuilds the text in the background |
| Point at a word, press the button  | Waits 1 second, then prints that word plus its sentence context    |
| Flip the toggle switch ON          | Prints detailed word, line, and paragraph insight, then waits      |
| Flip the toggle switch back OFF    | Returns to continuous reading                                      |

Keep your fingertip just below the word you want. The engine picks the word you are touching
first, and falls back to the direction your finger points if you are not touching one.

---

## 🧪 Part 6 — Running the Tests

From the project root:

```powershell
python -m pytest Gesture\tests -q
```

For detailed per-test output:

```powershell
python -m pytest Gesture\tests -v
```

---

## 🩹 Troubleshooting

| Problem | Fix |
| ------- | --- |
| `Activate.ps1 cannot be loaded` | Run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`, then activate again |
| `No module named cv2` / `mediapipe` | The venv isn't active. Run `.\.venv\Scripts\Activate.ps1` — your prompt should show `(.venv)` |
| `Cannot connect to ESP32` | Board off, wrong WiFi, or wrong IP/port in `.env`. Buttons use port **8080** |
| `GOOGLE_VISION_API_KEY is not set` | `.env` is missing or the key line is blank. See Part 3 |
| Camera URL shows nothing in browser | Re-flash the ESP32-CAM and check its IP in the Arduino serial monitor |
| VS Code shows red squiggles on imports | Reload the window. `Ctrl+Shift+P` → "Developer: Reload Window" |

---

## 📄 License

Part of the **TaleTrace** AI Smart Reading Companion Project.
