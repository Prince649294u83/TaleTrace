# TaleTrace Hardware Architecture & Protocol Specification

## 1. Physical Hardware Layout & Network Contracts

TaleTrace interfaces with two distinct physical microcontrollers over LAN:

```
                                  LOCAL NETWORK (LAN)
                                           │
                    ┌──────────────────────┴──────────────────────┐
                    │                                             │
                    ▼ :80                                         ▼ :8080
      ┌───────────────────────────┐                 ┌───────────────────────────┐
      │         ESP32-CAM         │                 │   ESP32 DevKit (Buttons)  │
      │  (Config-driven endpoint) │                 │   (Static 192.168.1.26)   │
      ├───────────────────────────┤                 ├───────────────────────────┤
      │ GET /capture              │                 │ GET /buttons              │
      │ -> Raw JPEG Frame bytes   │                 │ -> JSON Level State       │
      │ -> XGA 1024x768 (PSRAM)   │                 │ POST /display             │
      │ -> jpeg_hash verification │                 │ -> Raw text/plain Body    │
      │                           │                 │ GET /health (Optional)    │
      │                           │                 │ -> JSON Self-Describing IP│
      └─────────────┬─────────────┘                 └─────────────┬─────────────┘
```

### 1.1 ESP32 DevKit (Buttons & OLED) — `buttons_and_oled.ino`
- **Network**: Static IP `192.168.1.26:8080` (WiFi Station mode).
- **GPIO Pinout**:
  - `GPIO 4`: Momentary Push Button (`BTN_MOMENTARY_PIN`), `INPUT_PULLUP` (Active LOW).
  - `GPIO 5`: Toggle Switch (`BTN_TOGGLE_PIN`), `INPUT_PULLUP` (Active LOW).
- **OLED Display**:
  - SH1106 $128\times 64$ I2C (`U8G2_SH1106_128X64_NONAME_F_HW_I2C`).
  - Font: `u8g2_font_profont12_tf` ($\approx 18$ characters/line, 4 visible lines per page).
  - Buffer: `formattedLines[40]` ($\approx 720$ character maximum text buffer capacity).
- **Endpoints**:
  - `GET /buttons`: Returns level JSON `{"btn_momentary": bool, "btn_toggle": bool, "both_active": bool}`. The backend is the authoritative edge detector.
  - `POST /display`: Accepts raw `text/plain` body. Firmware formats and wraps lines into pages.
  - `GET /health`: Optional self-describing diagnostic JSON (`device`, `protocol_version`, `firmware_version`, `ip`, `port`, `uptime_ms`, `wifi_rssi`, `oled`).
- **Firmware-Owned Dual-Button Scrolling**:
  - When both buttons are active (`both_active == true`), firmware executes `scrollOLEDNext()` with a $250\text{ms}$ debounce.
  - The backend suppresses reading updates while the toggle is active, preventing accidental pointer advancement.

### 1.2 ESP32-CAM (Camera)
- **Resolution**: XGA ($1024\times 768$) or SVGA ($800\times 600$) in RGB/JPEG.
- **Endpoint**: `GET /capture` returns raw JPEG byte stream.
- **Freshness**: Tracked via `jpeg_hash` payload checksum to distinguish `NO_VISUAL_CHANGE` from `STALE_CAMERA`.

---

## 2. Dual-Identity Spatial Model (`PageContext` vs `FrameContext`)

- **`CoordinateSpace`**: Canonical spatial definition (`width`, `height`, `crop`, `rotation`, `mirror`).
- **`PageContext`**: Stable OCR word coordinates bound to a logical page (`page_id`, `page_version`, `geometry_hash`, `coordinate_space`, `page_region`, `ocr_words`).
- **`FrameContext`**: Discrete physical camera capture instance (`frame_id`, `page_id`, `captured_at`, `source_space`).
- **Low-Frequency Background Fingerprinting**:
  - Fingerprints are computed only on the `page_region` **excluding** the dynamic `gesture_region` (finger interaction area).
  - Moving fingers do not invalidate OCR cache.

---

## 3. Physical State & Event Mapping

| Physical Action | Polled Levels | Backend Event Generated | Reading Pointer | OLED Display State | Audio (TTS / Ambient) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Momentary Press (Toggle OFF)** | `momentary=True, toggle=False` | `READING_UPDATE_REQUESTED` | Advances to pointed line | No change | Continues reading |
| **Toggle Switch ON** | `momentary=False, toggle=True` | `MEANING_MODE_ON` | Pauses (frozen) | Awaits pointing | Pauses immediately |
| **Point at Word (Toggle ON)** | `momentary=False, toggle=True` | `WORD_SELECTED` $\to$ `MEANING_REQUESTED` | No change | Renders AI explanation | Plays word explanation |
| **Both Buttons Pressed (Toggle ON + Momentary)** | `momentary=True, toggle=True, both=True` | *None* (Suppressed by Backend) | No change | **Scrolls locally** on ESP32 | No change |
| **Toggle Switch OFF** | `momentary=False, toggle=False` | `MEANING_MODE_OFF` | Resumes from pointer | Retains last explanation | Resumes narration & ambient |

---

## 4. Hardware Failure Taxonomy & Circuit Breaker

The `HardwareSupervisor` manages independent 4-state state machines (`DISCONNECTED`, `READY`, `DEGRADED`, `RECOVERING`) for Camera, Buttons, and OLED:

- `CONNECT_TIMEOUT`: Device failed to respond to initial TCP connection within timeout.
- `READ_TIMEOUT`: Connection established but HTTP response timed out.
- `BAD_RESPONSE`: HTTP status code $\ge 400$ or invalid headers.
- `HTTP_ERROR`: Transport error or connection reset.
- `INVALID_PAYLOAD`: Malformed JSON or non-JPEG payload.
- `STALE_DATA`: Repeated identical payload across dynamic session requests.

Reading sessions require `Camera READY` AND `Button Device READY`. OLED availability is optional.
