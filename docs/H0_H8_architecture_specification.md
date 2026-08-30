# TaleTrace H0–H8 Architecture Specification & Permanent System Invariants

**Commit Baseline**: `09010fe346b167b73fb54d9a31341f8efccce3d5`  
**Authoritative Hardware Source**: `c:\Users\dhrut\Documents\dhruthi\taletrace_v7\TaleTrace\buttons_and_oled.ino`  
**ESP32-CAM Firmware Spec**: `c:\Users\dhrut\Documents\dhruthi\taletrace_v7\TaleTrace\backend\app\OCRandGESTURE\espcam\Almost_final.ino`  

---

## 1. Executive Context & Permanent Architectural Invariants

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│                   PERMANENT TALE-TRACE ARCHITECTURAL INVARIANT              │
├─────────────────────────────────────────────────────────────────────────────┤
│ • ESP32-CAM = Sensing + JPEG Compression + Local HTTP Server                │
│ • ESP32 Buttons/OLED = GPIO Pin Reading + OLED Render + Local HTTP Server   │
│ • Laptop = Brain & Compute (OpenCV, OCR, Groq AI, TTS, Ambient, SQLite)     │
│ • Local LAN Only = ESP32s never receive cloud keys and never touch Cloud    │
│ • Main Hardware File = c:\Users\dhrut\Documents\dhruthi\taletrace_v7\        │
│                        TaleTrace\buttons_and_oled.ino                       │
└─────────────────────────────────────────────────────────────────────────────┘
```

> [!IMPORTANT]
> **The Laptop-Offload Principle**:
> The laptop may perform arbitrarily expensive computation without increasing the computational workload of the ESP32; only local-network traffic caused by the laptop can indirectly affect the embedded devices.

### Forensic Distinction: Cloud APIs vs Embedded Physical Load
1. **Cloud APIs are 100% Laptop-Side**:
   - Groq API credentials (`GROQ_API_KEY_1`, `GROQ_API_KEY_2`, `GROQ_API_KEY_3`) are resolved exclusively in the laptop Python runtime.
   - OCR engines (Google Vision / OCR.Space) receive image bytes and execute HTTPS requests strictly from Python on the laptop.
   - Audio synthesis (Edge TTS / Offline SAPI5 WAV) and Ambient soundscapes (Pygame multi-channel mixer) execute entirely on the laptop host.
   - **The ESP32 devices never see, receive, or route cloud credentials.**
2. **True Root Cause of Physical Hardware Load**:
   - **ESP32-CAM**: Stressed by optical frame acquisition, JPEG compression, single-framebuffer allocation in PSRAM, and local Wi-Fi TCP/HTTP transmission over `GET /capture`.
   - **ESP32 Buttons/OLED**: Polled over local Wi-Fi via `GET /buttons` and updated via `POST /display`.
   - **Observed vs Hypothesized**: Observed symptom is camera/network timeout (`NO_FRAME`). Physical hardware damage is **NOT ESTABLISHED** and must not be asserted without direct electrical, thermal, and reset telemetry.

---

## 2. Phase H0: Direct Firmware Truth Audit

### A. Main Hardware Firmware: Buttons & OLED (`buttons_and_oled.ino`)
- **Role**: Primary physical user interaction and display peripheral.
- **Module**: ESP32 DevKit with SH1106 $128 \times 64$ I2C OLED display (`U8g2lib`).
- **Network Interface**: Static IP `192.168.1.26:8080`, local `WebServer.h`.
- **Hardware Pinout**:
  - GPIO 4: Push Button (`BTN_MOMENTARY_PIN`, `INPUT_PULLUP`) — Word pointing & trigger.
  - GPIO 5: Toggle Switch (`BTN_TOGGLE_PIN`, `INPUT_PULLUP`) — Meaning Mode latch.
  - I2C Bus: Hardware I2C for SH1106 OLED (`U8G2_SH1106_128X64_NONAME_F_HW_I2C`).
- **Active Endpoints**:
  - `GET /buttons`: Returns `{"btn_momentary": bool, "btn_toggle": bool, "both_active": bool}`.
  - `GET /health`: Returns `{"device": "button_oled", "protocol_version": 1, "firmware_version": "1.2.0", "ip": "...", "port": 8080, "uptime_ms": ..., "wifi_rssi": ..., "oled": true}`.
  - `POST /display`: Receives plain-text payload (up to 1,500 bytes), formats across 4 visible lines (~18 chars/line), and updates OLED with local scroll buffer.

### B. ESP32-CAM Firmware Specification (`Almost_final.ino`)
- **Role**: Optical image capture peripheral.
- **Module**: AI-Thinker ESP32-CAM with OV2640 optical sensor.
- **Network Interface**: Static IP `192.168.1.200:80`, local `esp_http_server.h`.
- **Active Endpoints**: Exactly one endpoint: `GET /capture`.
  - *Finding*: No lightweight `GET /health` endpoint currently exists on the camera firmware; camera health in Phase H1 is marked `UNAVAILABLE`.
- **Sensor, Clock & Buffer Parameters**:
  - `xclk_freq_hz`: $20,000,000\text{ Hz}$ ($20\text{ MHz}$)
  - `pixel_format`: `PIXFORMAT_JPEG`
  - `frame_size`: `FRAMESIZE_XGA` ($1024 \times 768$) with PSRAM, fallback `FRAMESIZE_SVGA` ($800 \times 600$)
  - `jpeg_quality`: `8` (PSRAM) / `10` (no PSRAM)
  - `fb_count`: `1` — Single framebuffer gives tight control and minimal PSRAM allocation.
  - `grab_mode`: Default `CAMERA_GRAB_WHEN_EMPTY`.
- **Power Configuration**: Brownout detector disabled via `WRITE_PERI_REG(RTC_CNTL_BROWN_OUT_REG, 0)`.

---

## 3. End-to-End Topology & Data-Flow Architecture

```text
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                        TALE-TRACE 5-LEVEL SYSTEM ARCHITECTURE                          │
├────────────────────────────────────────────────────────────────────────────────────────┤
│                                     INTERNET                                           │
│                 ▲                       ▲                     ▲                        │
│                 │ (HTTPS)               │ (HTTPS)             │ (HTTPS)                │
│                 ▼                       ▼                     ▼                        │
│          ┌──────────────┐       ┌───────────────┐     ┌───────────────┐                │
│          │  OCR Engine  │       │   Groq Cloud  │     │   Edge TTS    │                │
│          │ (Vision/Space│       │  Keys 1, 2, 3 │     │  (Narration)  │                │
│          └──────┬───────┘       └───────┬───────┘     └───────┬───────┘                │
│                 │                       │                     │                        │
│ ════════════════╪═══════════════════════╪═════════════════════╪═══════════════════════ │
│                 ▼                       ▼                     ▼                        │
│  LAPTOP BACKEND (Brain & Compute Engine)                                                │
│  ┌──────────────────────────────────────────────────────────────────────────────────┐  │
│  │ Level 1: Hardware Firewall Gateway (Priority-Aware Choke Point)                  │  │
│  │   • Priority Scheduling: P1 Gesture > P2 Reading OCR > P3 Diag > P4 Health       │  │
│  │   • CameraGateway: MAX_INFLIGHT=1, QUEUE_SIZE=1, Latest-Valid-Frame Semantics    │  │
│  │   • Single-Capture-Owner Tracking: reading_scheduler, gesture_pipeline, diag    │  │
│  │   • ButtonGateway: Session Connection Reuse, Edge Detection (10-20 Hz)           │  │
│  │   • DisplayGateway: Event-Driven Plaintext POSTs (Only on Lookup Resolution)     │  │
│  │   • HealthMonitor: Cached Background Polling (0.1 Hz), Decoupled from /capture   │  │
│  ├──────────────────────────────────────────────────────────────────────────────────┤  │
│  │ Level 2: Core Processing (Laptop Local Compute)                                  │  │
│  │   • OpenCV Decode (JPEG -> RGB), Deskew & Canonical Transform (1200x1600)        │  │
│  │   • MediaPipe Hands + Partial-Finger Contour Fallback + Margin Filtering         │  │
│  │   • Google Vision / OCR.Space Exact Bounding Box Geometry                        │  │
│  │   • Reading Engine: Pointer Advancement, Word Highlighting, Session Lifecycle    │  │
│  ├──────────────────────────────────────────────────────────────────────────────────┤  │
│  │ Level 3: AI Intelligence (Laptop Cloud Calls)                                    │  │
│  │   • Meaning Mode AI (Key 1): One-Word Explanations & Session Review Payloads     │  │
│  │   • Merge Memory Reconstructor (Key 2): 850-Token Cap & 1.5s 429 Retry Budget    │  │
│  │   • Learning Engine (Key 3): Vocabulary Metrics & Takeaways                      │  │
│  │   • Scene Controller: Scene Mood Classification & Audio Theme Transitions        │  │
│  ├──────────────────────────────────────────────────────────────────────────────────┤  │
│  │ Level 4: Audio Output (Local to Laptop Host)                                     │  │
│  │   • Edge TTS Narration + SAPI5 Offline WAV Stream Fallback (LocalAudioSink)      │  │
│  │   • Ambient Soundscape Engine (Pygame Multi-Channel Mixer)                       │  │
│  │   • Option B Audio Invariant: Meaning Mode Instant Parallel Pause & Resume       │  │
│  │   • Strict Prohibition: ESP32 is NOT an audio transport; 0 audio over Wi-Fi     │  │
│  ├──────────────────────────────────────────────────────────────────────────────────┤  │
│  │ Level 5: Persistence & Companion API                                             │  │
│  │   • SQLite taletrace.db (Zero data drop, naive UTC normalization)                │  │
│  │   • FastAPI Companion Routes (Cached Device Status, Quizzes, Flashcards)         │  │
│  └──────────────────────────┬────────────────────────────┬──────────────────────────┘  │
│                             │                            │                             │
│ ════════════════════════════╪════════════════════════════╪════════════════════════════ │
│                             │ (Local Wi-Fi LAN Only)     │ (Local Wi-Fi LAN Only)      │
│                             ▼                            ▼                             │
│                   ┌───────────────────┐        ┌───────────────────┐                   │
│                   │     ESP32-CAM     │        │ ESP32 Button/OLED │                   │
│                   │ (192.168.1.200:80)│        │(192.168.1.26:8080)│                   │
│                   │ • Sensor Capture  │        │ • GPIO 4 / GPIO 5 │                   │
│                   │ • JPEG Encode     │        │ • SH1106 Display  │                   │
│                   │ • Single FB Lock  │        │ • GET /buttons    │                   │
│                   │ • GET /capture    │        │ • POST /display   │                   │
│                   │ • GET /health (H2)│        │ • GET /health     │                   │
│                   └───────────────────┘        └───────────────────┘                   │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 4. Hardware Firewall Gateway Architectural Design (Phase H2 Reference)

### A. Priority-Aware Scheduling & Strict Backpressure (`MAX_INFLIGHT=1`, `QUEUE_SIZE=1`)
- **Rule 1 (Single Active Request Guarantee)**: Exactly one `GET /capture` request in-flight to ESP32-CAM at any instant.
- **Rule 2 (Priority-Aware Queue Policy)**:
  - Priority 1 (Meaning Mode / Gesture Burst): Supersedes pending normal reading OCR requests.
  - Priority 2 (Normal Reading OCR): Scheduled page tracking.
  - Priority 3 (Diagnostics): On-demand diagnostic probes.
  - Priority 4 (Health Monitoring): Prohibited from claiming `/capture` once `/health` is operational.
- **Rule 3 (Queue Size = 1 with Latest-Valid-Frame Semantics)**:
  - Queue holds at most 1 pending request; new requests supersede older stale ones.
- **Rule 4 (No Downstream Consumer = No Capture)**:
  - Meaning Mode active $\to$ continuous capture suspended ($0\text{ FPS}$).
  - OLED scrolling $\to$ camera capture suspended.
  - Companion dashboard refresh $\to$ camera capture forbidden (served from health cache).
- **Rule 5 (Hardware Firewall Boundary)**:
  - Production components (`ReadingEngine`, `DeviceLoop`, `AiBridge`) must route through `HardwareGateway`.

---

## 5. Separate Resource Budgets (Initial Target Operating Limits)

### A. Initial Target Embedded Resource Budget (ESP32 Peripherals)
```text
┌─────────────────────────────────────────────────────────────────────────────┐
│                 INITIAL TARGET EMBEDDED RESOURCE BUDGET                     │
├──────────────────────┬──────────────────────────────────────────────────────┤
│ Subsystem            │ Target Operating Candidates (Pending H1-H7 Measures) │
├──────────────────────┼──────────────────────────────────────────────────────┤
│ ESP32-CAM Capture    │ Continuous Cadence Target: ~0.5 – 1.0 FPS            │
│                      │ In-Flight Limit: MAX_INFLIGHT = 1                    │
│                      │ Queue Policy: QUEUE_SIZE = 1 (Latest-Valid-Frame)    │
│                      │ Priority: P1 Gesture > P2 Reading OCR > P3 Diag      │
│                      │ Gesture Burst: 3 sequential single-flight frames     │
│                      │ Meaning Mode: 0 FPS (Capture completely suspended)   │
│                      │ Circuit Breaker: Candidate trip at 5 failures / 10s  │
├──────────────────────┼──────────────────────────────────────────────────────┤
│ ESP32 Buttons / OLED │ Polling Frequency Target: 10 – 20 Hz (50 – 100 ms)   │
│ (buttons_and_oled)   │ Connection: Persistent Session / Connection Reuse    │
│                      │ Display POSTs: Event-driven on lookup result only    │
│                      │ Health Polling: Background cached (0.1 Hz / 10s)     │
│                      │ Audio Streaming: STRICTLY PROHIBITED (0 streams)     │
└──────────────────────┴──────────────────────────────────────────────────────┘
```

### B. Initial Target Cloud / Laptop API Budget
```text
┌─────────────────────────────────────────────────────────────────────────────┐
│                 INITIAL TARGET CLOUD & LAPTOP API BUDGET                    │
├──────────────────────┬──────────────────────────────────────────────────────┤
│ Subsystem            │ Operating Envelope (Laptop Host & Internet)          │
├──────────────────────┼──────────────────────────────────────────────────────┤
│ OCR Engine           │ Google Vision / OCR.Space: Triggered on Page Turns   │
│ Groq Merge Memory    │ GROQ_API_KEY_2: max_completion_tokens=850, 1.5s max  │
│ Groq AI Engine       │ GROQ_API_KEY_1: Event-driven Meaning Mode & Quizzes  │
│ Groq Learning Engine │ GROQ_API_KEY_3: End-of-session vocabulary assessment │
│ Speech Narration     │ Edge TTS / SAPI5 Offline WAV to LocalAudioSink       │
│ Ambient Soundscape   │ Local Pygame mixer (0 network bandwidth)             │
└──────────────────────┴──────────────────────────────────────────────────────┘
```

---

## 6. Phased Research & Execution Protocol (Phases H0 to H8)

- **Phase H0**: Firmware Truth Audit (Completed).
- **Phase H1**: Baseline Load Profiling (Current Active Phase — Strictly Read-Only).
- **Phase H2**: Gateway Architecture (Frozen: Waiting for H1 Evidence).
- **Phase H3**: Cloud Decoupling Proof (WAN isolation & firmware security scan).
- **Phase H4**: Cadence Optimization (A/B evaluation of 0.5 vs 0.75 vs 1.0 FPS).
- **Phase H5**: Gesture Burst Protocol (3-frame sequential burst).
- **Phase H6**: Resolution Experiment (XGA vs SVGA trade-off).
- **Phase H7**: Physical Power, Thermal & Stability Diagnostics.
- **Phase H8**: Final Hardware Budget Freeze & Laptop Profiling.
