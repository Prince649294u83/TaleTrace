"""Statistically rigorous ESP32-CAM diagnostic script.

Executes Phases A, B, C, D, captures raw latency samples, decodes every JPEG
with OpenCV, extracts pixel dimensions, and calculates documented quantiles
(min, P50, P90, P95, P99, max) with raw data persisted to JSON.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import requests

CAMERA_URL = "http://192.168.1.200/capture"
BUTTONS_URL = "http://192.168.1.26:8080/buttons"
OUT_DIR = Path("scratch/camera_diagnostics")
OUT_DIR.mkdir(parents=True, exist_ok=True)


def capture_single_frame(session: requests.Session, timeout: float = 5.0) -> dict[str, Any]:
    t0 = time.perf_counter()
    status_code = None
    exception_type = None
    raw_bytes = b""
    decode_ok = False
    dims = None
    
    try:
        resp = session.get(CAMERA_URL, timeout=timeout)
        status_code = resp.status_code
        if resp.status_code == 200:
            raw_bytes = resp.content
            if raw_bytes:
                arr = np.frombuffer(raw_bytes, dtype=np.uint8)
                img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if img is not None and img.size > 0:
                    decode_ok = True
                    dims = [int(img.shape[1]), int(img.shape[0])]  # [width, height]
    except Exception as e:
        exception_type = type(e).__name__
    t1 = time.perf_counter()
    
    latency_ms = (t1 - t0) * 1000.0
    return {
        "latency_ms": round(latency_ms, 2),
        "status_code": status_code,
        "bytes": len(raw_bytes),
        "decode_ok": decode_ok,
        "dimensions": dims,
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


def run_phase(name: str, count: int, delay_sec: float) -> dict[str, Any]:
    print(f"\n--- Running {name} ({count} captures, {delay_sec}s delay) ---")
    session = requests.Session()
    records = []
    
    for i in range(1, count + 1):
        rec = capture_single_frame(session)
        rec["frame_id"] = i
        records.append(rec)
        if delay_sec > 0:
            time.sleep(delay_sec)
            
    valid_latencies = [r["latency_ms"] for r in records if r["decode_ok"]]
    stats = compute_quantiles(valid_latencies)
    fails = count - len(valid_latencies)
    
    print(f"Results for {name}:")
    print(f"  Valid JPEGs: {len(valid_latencies)}/{count} (Failures: {fails})")
    if stats:
        print(f"  Min: {stats['min']}ms | P50: {stats['p50']}ms | P95: {stats['p95']}ms | P99: {stats['p99']}ms | Max: {stats['max']}ms")
        
    return {"phase": name, "stats": stats, "failures": fails, "samples": records}


def run_contention_phase(count: int = 30) -> dict[str, Any]:
    """Phase D: Trigger concurrent overlapping button polls while requesting frame captures."""
    from concurrent.futures import ThreadPoolExecutor
    print(f"\n--- Running Phase_D_Contention ({count} captures with overlapping concurrent button polling) ---")
    session = requests.Session()
    records = []
    
    def poll_buttons() -> None:
        try:
            session.get(BUTTONS_URL, timeout=1.0)
        except Exception:
            pass

    for i in range(1, count + 1):
        with ThreadPoolExecutor(max_workers=2) as executor:
            # Overlap button request and camera capture simultaneously in parallel threads
            fut_btn = executor.submit(poll_buttons)
            fut_cam = executor.submit(capture_single_frame, session, 5.0)
            
            rec = fut_cam.result()
            fut_btn.result()  # Ensure button polling thread joined
            
        rec["frame_id"] = i
        records.append(rec)
        time.sleep(0.1)  # 100ms cadence between contention bursts
        
    valid_latencies = [r["latency_ms"] for r in records if r["decode_ok"]]
    stats = compute_quantiles(valid_latencies)
    fails = count - len(valid_latencies)
    
    print("Results for Phase_D_Contention:")
    print(f"  Valid JPEGs: {len(valid_latencies)}/{count} (Failures: {fails})")
    if stats:
        print(f"  Min: {stats['min']}ms | P50: {stats['p50']}ms | P95: {stats['p95']}ms | P99: {stats['p99']}ms | Max: {stats['max']}ms")
        
    return {"phase": "Phase_D_Contention", "stats": stats, "failures": fails, "samples": records}


if __name__ == "__main__":
    p_a = run_phase("Phase_A_Smoke", count=10, delay_sec=0.2)
    p_b = run_phase("Phase_B_Distribution", count=100, delay_sec=0.05)
    p_c = run_phase("Phase_C_Cadence", count=100, delay_sec=1.0)
    p_d = run_contention_phase(count=30)
    
    report = {"phases": [p_a, p_b, p_c, p_d]}
    out_file = OUT_DIR / "camera_statistical_report.json"
    with open(out_file, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nRaw report persisted to: {out_file}")
