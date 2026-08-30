# Physical Incident Report: INC-20260830-01 (ESP32-CAM Layer 0 Thermal Observation)

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│                    PHYSICAL INCIDENT TELEMETRY RECORD                       │
├─────────────────────────────────────────────────────────────────────────────┤
│ Incident ID:        INC-20260830-01                                         │
│ Date / Timestamp:   2026-08-30T17:13:00+05:30                               │
│ Target Peripheral:  AI-Thinker ESP32-CAM (OV2640, IP: 192.168.1.200)        │
│ Test Phase:         Phase H1 Baseline Measurement                           │
│ Test Layer:         Layer 0: Idle Observation                               │
│ Trigger:            Operator observed physical thermal rise during idle     │
│ Classification:     PHYSICAL THERMAL OBSERVATION (Subjective until measured)│
│ Action Taken:       IMMEDIATE PHYSICAL DISCONNECT (Power Removed)           │
│ Immediate Symptom:  Excessive observed physical heat during idle interval   │
│ Current Status:     SAFETY HOLD / INVESTIGATION REQUIRED                    │
│ Established Cause:  UNKNOWN (Candidate hypotheses pending T0–T2 tests)     │
│ Next Action:        Execute Stage T0 (Visual) + Stage T1 (Power/Current)    │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 1. Forensic Context & Evidence Evaluation

1. **Test Invariant Verification**:
   - Layer 0 diagnostic code issued **exactly 0 HTTP requests to `GET /capture`** (`camera_http_request_count = 0`).
   - The laptop executed 0 OCR calls, 0 Groq API calls, 0 TTS audio streams, and 0 database writes.
   - **Empirical Finding**: For this Layer 0 event, cloud API processing was not occurring in the TaleTrace process, and no camera capture was requested.

2. **Forensic Boundary**:
   - The thermal event was observed during the embedded baseline state; the specific thermal source and root cause remain unresolved.
   - Cloud API credentials (`GROQ_API_KEY_1`, `GROQ_API_KEY_2`, `GROQ_API_KEY_3`, Google Vision) execute strictly on the laptop host and have zero direct connection to the ESP32 power rails.
   - Rapid JPEG frame acquisition loops were not executing during this observation interval.

3. **What Remains Unproven (Zero Speculation)**:
   - Whether the heat originates from the **AMS1117-3.3V linear regulator**, the **ESP32 SoC package**, the **OV2640 sensor module**, the **flash/PSRAM IC**, or an **external power-supply/cabling droop**.
   - Whether physical hardware degradation occurred.
   - Whether disabling the brownout detector (`WRITE_PERI_REG(RTC_CNTL_BROWN_OUT_REG, 0)`) masked power-rail voltage collapse under Wi-Fi RF load.

---

## 2. Electrical & Thermal Schematic Context (AI-Thinker Architecture)

The official AI-Thinker ESP32-CAM schematic establishes:
- The board's 5V input rail feeds an onboard **AMS1117-3.3V linear voltage regulator**, which supplies the ESP32 module, flash, and PSRAM.
- Linear regulator power dissipation follows $P_{\text{regulator}} \approx (V_{\text{in}} - V_{\text{out}}) \times I_{\text{3.3V}}$. At $V_{\text{in}} = 5.0\text{V}$ and $V_{\text{out}} = 3.3\text{V}$, $\Delta V = 1.7\text{V}$.
- Espressif specifications specify the ESP32 module operating range as $3.0\text{V} – 3.6\text{V}$ at the module level and recommend a 3.3V supply capable of at least $500\text{mA}$ peak current during RF transmission bursts.
- **Power Supply Requirement**: Verify that the selected supply has adequate voltage regulation and current headroom for the board and attached peripherals; do not interpret a supply's rated 2 A capacity as the board's actual consumption.

---

## 3. Mandatory Physical Diagnostic Checklist (Stage T0 & Stage T1)

Before applying power to the ESP32-CAM again, record the following physical observations:

| Parameter | Observed Physical Value | Measurement Method |
|---|---|---|
| **Board Identification** | AI-Thinker ESP32-CAM / Clone / S3-CAM | Visual inspection of PCB silkscreen |
| **Camera Sensor** | OV2640 / OV3660 / Other | Visual inspection of sensor module & ribbon |
| **Power Supply** | Dedicated 5V Regulated Supply / USB Port / FTDI | Direct inspection of power source |
| **Supply Capability** | Rated V and A (Verify adequate headroom) | Supply label / specification |
| **Cable Type & Gauge** | USB Cable / Jumper Wires (Length & Gauge) | Physical inspection |
| **Supply Voltage (5V Rail at Board)** | ___ V (Idle) / ___ V (Active) | Multimeter reading between 5V pin and GND |
| **Regulated Voltage (3.3V Rail)** | ___ V (Idle) / ___ V (Active) | Multimeter reading between 3V3 pin and GND |
| **Current Draw (5V Rail)** | ___ mA | Multimeter in series with 5V line |
| **Ambient Temperature** | ___ °C | Room thermometer / environmental probe |
| **Hotspot Location** | Regulator / ESP32 SoC / OV2640 / PSRAM | Non-contact IR thermometer / thermal camera |
| **Time to Thermal Event** | ___ seconds from power-on | Monotonic stopwatch |
| **Visual / Olfactory Signs** | None / Odor / Discoloration / Residue | Visual and olfactory inspection |
| **UART0 Serial Logging** | Available / Unavailable (Save boot output) | USB-UART bridge to `logs/h1t/serial_T1.txt` |

---

## 4. Phase H1-T Staged Isolation Protocol

Do **NOT** re-run full diagnostic suites. Proceed strictly through these micro-stages with full cooldown between stages:

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│                      PHASE H1-T STAGED ISOLATION PROTOCOL                   │
├─────────┬─────────────────────────┬─────────────────────────────────────────┤
│ Stage T0│ Cooldown & Inspection   │ Full ambient cooldown & visual check    │
│         │ (UNPOWERED)             │ Inspect PCB, regulator, solder, ribbon  │
├─────────┼─────────────────────────┼─────────────────────────────────────────┤
│ Stage T1│ Power & Current Profile │ Measure 5V, 3.3V, series current.       │
│         │ (INITIAL POWER TEST)    │ ───► CURRENT IMMEDIATE NEXT STEP ◄───   │
├─────────┼─────────────────────────┼─────────────────────────────────────────┤
│ Stage T2│ Controlled Idle (No /cap│ Wi-Fi up, 0 captures; measure temp slope│
│         │ (30s -> 60s -> 5 min)   │ at 0s, 10s, 30s, 60s, 120s, 300s.       │
├─────────┼─────────────────────────┼─────────────────────────────────────────┤
│ Stage T3│ Isolated Single Captures│ 1 capture / 10s (10 captures max).      │
│         │ (Conditional on T2 PASS)│ Measure temperature delta & latency.    │
├─────────┼─────────────────────────┼─────────────────────────────────────────┤
│ Stage T4│ Controlled Cadence (30s)│ 1 capture / sec for 30 seconds only.    │
├─────────┼─────────────────────────┼─────────────────────────────────────────┤
│ Stage T5│ Contention Verification │ 30s run with concurrent button polling. │
├─────────┼─────────────────────────┼─────────────────────────────────────────┤
│ Stage T6│ Controlled Full H1 Rerun│ ONLY executed if Stages T0–T5 pass.     │
└─────────┴─────────────────────────┴─────────────────────────────────────────┘
```

### Critical Stop Rules & Voltage Flags:
1. **T2 Abnormal Rule**: If Stage T2 (idle Wi-Fi, 0 captures) produces abnormal or rapid heating, **STOP IMMEDIATELY**. Do not proceed to capture testing (Stage T3).
2. **Immediate Power Removal**: Cut power immediately if unexpected rapid temperature rise, smoke, odor, PCB discoloration, or continuous reboot loops occur.
3. **Team-Defined Experimental Voltage Flags**: 5V rail $<4.75\text{V}$ or 3.3V rail $<3.15\text{V}$ serve as conservative experimental stop thresholds; confirm against exact board/regulator specifications before treating as product limits.
4. **Inter-Stage Cooldown**: Board must return to ambient temperature before starting each subsequent stage.
5. **Serial Boot Logging**: Capture UART0 serial boot logs for each stage and save to `logs/h1t/serial_<stage>.txt`.

---

## 5. Candidate Diagnostic Hypotheses (To Be Empirically Tested)

- **Hypothesis A (Normal Idle, Heat Rises on Capture)**: Suggests optical sensor power draw, PSRAM DMA activity, or JPEG compression engine workload as primary contributors.
- **Hypothesis B (Heat Rises in Stage T2 with 0 Captures)**: Suggests the issue is independent of capture frequency; narrows investigation to Wi-Fi RF power, linear regulator dissipation, or idle firmware state.
- **Hypothesis C (Heat Rises on Power-Only before Wi-Fi)**: Suggests electrical hardware fault, short circuit, or damaged linear regulator.
- **Hypothesis D (Stable Temperature but Voltage Dips)**: Suggests inadequate power supply regulation, high-resistance cabling, or brownout risk.
- **Hypothesis E (Stable Power & Thermal, but ESP32 Reboots)**: Suggests software watchdog, stack overflow, or memory corruption.
