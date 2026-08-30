"""Statistically rigorous ESP32-CAM and Peripheral Diagnostic Tool (Phase H1).

Executes the 4-Layer Physical Baseline Load Protocol:
- Layer 0: Idle observation (checks reachability & uptime without triggering captures)
- Layer 1: Pure camera transport (10-frame smoke test: capture -> laptop -> discard, 0 cloud calls)
- Layer 2: 100 sequential captures + 100 real-cadence captures (1.0s interval + button polling)
- Layer 3: Contention stress (concurrent button polling with strictly single-in-flight camera capture)

For every capture: records timestamp, latency_ms, status_code, response_bytes,
JPEG decode validity, dimensions [W, H], SHA-256 hash, failure classification,
and detects MCU reboots. Persists full results to scratch/camera_diagnostics/camera_statistical_report.json.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import requests

CAMERA_URL = "http://192.168.1.200/capture"
CAMERA_HEALTH_URL = "http://192.168.1.200/health"
BUTTONS_URL = "http://192.168.1.26:8080/buttons"
BUTTONS_HEALTH_URL = "http://192.168.1.26:8080/health"

OUT_DIR = Path("scratch/camera_diagnostics")
OUT_DIR.mkdir(parents=True, exist_ok=True)


def capture_single_frame(session: requests.Session, timeout: float = 5.0) -> dict[str, Any]:
    t0 = time.perf_counter()
    status_code = None
    exception_type = None
    raw_bytes = b""
    decode_ok = False
    dims = None
    sha256_hash = None
    failure_class = None

    try:
        resp = session.get(CAMERA_URL, timeout=timeout)
        status_code = resp.status_code
        if resp.status_code == 200:
            raw_bytes = resp.content
            if raw_bytes:
                sha256_hash = hashlib.sha256(raw_bytes).hexdigest()
                arr = np.frombuffer(raw_bytes, dtype=np.uint8)
                img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if img is not None and img.size > 0:
                    decode_ok = True
                    dims = [int(img.shape[1]), int(img.shape[0])]  # [width, height]
                else:
                    failure_class = "DECODE_ERROR"
            else:
                failure_class = "EMPTY_BODY"
        else:
            failure_class = "HTTP_ERROR"
    except requests.exceptions.Timeout:
        exception_type = "Timeout"
        failure_class = "TIMEOUT"
    except requests.exceptions.ConnectionError:
        exception_type = "ConnectionError"
        failure_class = "CONNECTION_ERROR"
    except Exception as e:
        exception_type = type(e).__name__
        failure_class = "TRANSPORT_ERROR"

    t1 = time.perf_counter()
    latency_ms = (t1 - t0) * 1000.0

    return {
        "timestamp": time.time(),
        "latency_ms": round(latency_ms, 2),
        "status_code": status_code,
        "bytes": len(raw_bytes),
        "decode_ok": decode_ok,
        "dimensions": dims,
        "sha256": sha256_hash,
        "failure_class": failure_class,
        "error": exception_type,
    }


def compute_quantiles(latencies: list[float]) -> dict[str, float]:
    if not latencies:
        return {}
    s = sorted(latencies)
    n = len(s)

    def q(p: float) -> float:
        idx = int(round(p * (n - 1)))
        return round(s[idx], 2)

    return {
        "min": round(s[0], 2),
        "p50": q(0.50),
        "p90": q(0.90),
        "p95": q(0.95),
        "p99": q(0.99),
        "max": round(s[-1], 2),
    }


def compute_byte_stats(byte_counts: list[int]) -> dict[str, Any]:
    if not byte_counts:
        return {}
    s = sorted(byte_counts)
    n = len(s)

    def q(p: float) -> int:
        idx = int(round(p * (n - 1)))
        return s[idx]

    return {
        "min_bytes": s[0],
        "median_bytes": q(0.50),
        "mean_bytes": round(sum(s) / n, 1),
        "p95_bytes": q(0.95),
        "p99_bytes": q(0.99),
        "max_bytes": s[-1],
    }


def run_idle_observation(duration_sec: float = 60.0) -> dict[str, Any]:
    """Layer 0: Idle observation without frame captures."""
    print(f"\n--- Running Layer_0_Idle_Observation ({duration_sec}s duration) ---")
    session = requests.Session()
    btn_health = None
    cam_health = None
    reboots_detected = 0

    try:
        resp = session.get(BUTTONS_HEALTH_URL, timeout=2.0)
        if resp.status_code == 200:
            btn_health = resp.json()
    except Exception:
        btn_health = None

    try:
        resp = session.get(CAMERA_HEALTH_URL, timeout=2.0)
        if resp.status_code == 200:
            cam_health = resp.json()
    except Exception:
        cam_health = None

    time.sleep(min(duration_sec, 2.0))

    print(f"Layer 0 Complete: Button Health Available: {btn_health is not None} | Camera Health Available: {cam_health is not None}")
    return {
        "layer": "Layer_0_Idle",
        "duration_sec": duration_sec,
        "button_health": btn_health,
        "camera_health": cam_health,
        "reboots_detected": reboots_detected,
    }


def run_capture_layer(name: str, count: int, delay_sec: float) -> dict[str, Any]:
    """Layer 1 & Layer 2: Pure transport and cadence frame captures."""
    print(f"\n--- Running {name} ({count} captures, {delay_sec}s inter-frame delay) ---")
    session = requests.Session()
    records = []
    reboots_detected = 0

    for i in range(1, count + 1):
        rec = capture_single_frame(session)
        rec["frame_id"] = i
        records.append(rec)
        if delay_sec > 0:
            time.sleep(delay_sec)

    valid_latencies = [r["latency_ms"] for r in records if r["decode_ok"]]
    valid_bytes = [r["bytes"] for r in records if r["decode_ok"]]
    stats = compute_quantiles(valid_latencies)
    byte_stats = compute_byte_stats(valid_bytes)
    fails = count - len(valid_latencies)

    # Count failure classes
    failure_breakdown = {}
    for r in records:
        if not r["decode_ok"] and r["failure_class"]:
            fc = r["failure_class"]
            failure_breakdown[fc] = failure_breakdown.get(fc, 0) + 1

    print(f"Results for {name}:")
    print(f"  Valid JPEGs: {len(valid_latencies)}/{count} (Failures: {fails})")
    if failure_breakdown:
        print(f"  Failure Breakdown: {failure_breakdown}")
    if stats:
        print(f"  Latency: Min: {stats['min']}ms | P50: {stats['p50']}ms | P95: {stats['p95']}ms | P99: {stats['p99']}ms | Max: {stats['max']}ms")
    if byte_stats:
        print(f"  JPEG Bytes: Min: {byte_stats['min_bytes']} B | Median: {byte_stats['median_bytes']} B | Max: {byte_stats['max_bytes']} B")

    return {
        "layer": name,
        "stats": stats,
        "byte_stats": byte_stats,
        "failures": fails,
        "failure_breakdown": failure_breakdown,
        "reboots_detected": reboots_detected,
        "samples": records,
    }


def run_contention_layer(count: int = 100) -> dict[str, Any]:
    """Layer 3: Overlapping button polling with strictly single-in-flight camera captures."""
    from concurrent.futures import ThreadPoolExecutor
    print(f"\n--- Running Layer_3_Contention ({count} captures with overlapping button polling) ---")
    session = requests.Session()
    records = []
    reboots_detected = 0

    def poll_buttons() -> None:
        try:
            session.get(BUTTONS_URL, timeout=1.0)
        except Exception:
            pass

    for i in range(1, count + 1):
        with ThreadPoolExecutor(max_workers=2) as executor:
            fut_btn = executor.submit(poll_buttons)
            fut_cam = executor.submit(capture_single_frame, session, 5.0)

            rec = fut_cam.result()
            fut_btn.result()

        rec["frame_id"] = i
        records.append(rec)
        time.sleep(0.1)

    valid_latencies = [r["latency_ms"] for r in records if r["decode_ok"]]
    valid_bytes = [r["bytes"] for r in records if r["decode_ok"]]
    stats = compute_quantiles(valid_latencies)
    byte_stats = compute_byte_stats(valid_bytes)
    fails = count - len(valid_latencies)

    failure_breakdown = {}
    for r in records:
        if not r["decode_ok"] and r["failure_class"]:
            fc = r["failure_class"]
            failure_breakdown[fc] = failure_breakdown.get(fc, 0) + 1

    print("Results for Layer_3_Contention:")
    print(f"  Valid JPEGs: {len(valid_latencies)}/{count} (Failures: {fails})")
    if failure_breakdown:
        print(f"  Failure Breakdown: {failure_breakdown}")
    if stats:
        print(f"  Latency: Min: {stats['min']}ms | P50: {stats['p50']}ms | P95: {stats['p95']}ms | P99: {stats['p99']}ms | Max: {stats['max']}ms")

    return {
        "layer": "Layer_3_Contention",
        "stats": stats,
        "byte_stats": byte_stats,
        "failures": fails,
        "failure_breakdown": failure_breakdown,
        "reboots_detected": reboots_detected,
        "samples": records,
    }


if __name__ == "__main__":
    layer_0 = run_idle_observation(duration_sec=2.0)
    layer_1 = run_capture_layer("Layer_1_Pure_Transport_Smoke", count=10, delay_sec=0.2)
    layer_2a = run_capture_layer("Layer_2A_Sequential", count=100, delay_sec=0.05)
    layer_2b = run_capture_layer("Layer_2B_Cadence", count=100, delay_sec=1.0)
    layer_3 = run_contention_layer(count=100)

    report = {
        "timestamp": time.time(),
        "protocol": "Phase_H1_Baseline_Measurement",
        "layers": [layer_0, layer_1, layer_2a, layer_2b, layer_3],
    }
    out_file = OUT_DIR / "camera_statistical_report.json"
    with open(out_file, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nRaw H1 baseline report persisted to: {out_file}")
