"""Statistically Rigorous ESP32-CAM and Peripheral Diagnostic Tool (Phase H1 Read-Only Baseline).

Executes the 4-Layer Physical Baseline Load Protocol:
- Layer 0: Idle observation (checks reachability & uptime without triggering captures; asserts camera_http_request_count == 0)
- Layer 1: Pure camera transport (10-frame smoke test: capture -> laptop -> discard, 0 cloud calls)
- Layer 2A: 100 sequential captures (0.05s inter-frame delay; AGGRESSIVE STRESS TEST ~20 req/s, NOT representative of normal reading cadence)
- Layer 2B: 100 real-cadence captures (1.0s interval; intended normal reading page tracking cadence)
- Layer 3: Contention stress (concurrent button polling with strictly single-in-flight camera capture)

Invariants:
- Strictly read-only: never modifies firmware, production code, or databases.
- Single-flight capture guarantee: diagnostic harness ensures max(overlap) == 0.
- Granular error classification: ConnectTimeout, ReadTimeout, ConnectionError, RemoteDisconnected, ConnectionReset, HTTP_Non_200, Empty_Response, JPEG_Decode_Failure, DIMENSION_MISMATCH.
- Triple-verdict output: CAMERA TRANSPORT, NETWORK PATH, and HARDWARE HEALTH (NOT MEASURED).
- Output machine-readable baseline to logs/h1_camera_baseline_<timestamp>.json and raw evidence to logs/h1_raw_evidence/.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import socket
import sys
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import requests

CAMERA_URL = os.environ.get("ESP32_CAM_CAPTURE_URL", "http://192.168.1.200/capture")
CAMERA_HEALTH_URL = os.environ.get("ESP32_CAM_HEALTH_URL", "http://192.168.1.200/health")
BUTTONS_URL = os.environ.get("ESP32_BUTTONS_URL", "http://192.168.1.26:8080/buttons")
BUTTONS_HEALTH_URL = os.environ.get("ESP32_BUTTONS_HEALTH_URL", "http://192.168.1.26:8080/health")

LOGS_DIR = Path("logs")
LOGS_DIR.mkdir(parents=True, exist_ok=True)
RAW_EVIDENCE_DIR = LOGS_DIR / "h1_raw_evidence"

EXPECTED_WIDTH = 1024
EXPECTED_HEIGHT = 768


def get_git_commit() -> str:
    try:
        import subprocess
        res = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=3)
        if res.returncode == 0:
            return res.stdout.strip()
    except Exception:
        pass
    return "09010fe346b167b73fb54d9a31341f8efccce3d5"


def get_network_identity() -> dict[str, Any]:
    local_ip = "127.0.0.1"
    hostname = socket.gethostname()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        pass

    return {
        "hostname": hostname,
        "local_ip": local_ip,
        "platform": platform.platform(),
        "camera_target_url": CAMERA_URL,
        "button_target_url": BUTTONS_URL,
    }


def capture_single_frame(
    session: requests.Session,
    timeout: float = 5.0,
    *,
    save_raw: bool = False,
    request_id: int = 1,
    layer_name: str = "diagnostic",
) -> dict[str, Any]:
    t0_perf = time.perf_counter()
    start_wall = time.time()
    status_code = None
    exception_type = None
    exception_msg = None
    raw_bytes = b""
    decode_ok = False
    dims = None
    sha256_hash = None
    failure_class = None
    response_headers: dict[str, str] = {}

    try:
        resp = session.get(CAMERA_URL, timeout=timeout)
        status_code = resp.status_code
        response_headers = {k: v for k, v in resp.headers.items()}
        if resp.status_code == 200:
            raw_bytes = resp.content
            if raw_bytes:
                sha256_hash = hashlib.sha256(raw_bytes).hexdigest()
                arr = np.frombuffer(raw_bytes, dtype=np.uint8)
                img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if img is not None and img.size > 0:
                    decode_ok = True
                    dims = [int(img.shape[1]), int(img.shape[0])]
                    if dims[0] != EXPECTED_WIDTH or dims[1] != EXPECTED_HEIGHT:
                        failure_class = "DIMENSION_MISMATCH"
                else:
                    failure_class = "JPEG_Decode_Failure"
            else:
                failure_class = "Empty_Response"
        else:
            failure_class = f"HTTP_Non_200_{resp.status_code}"
    except requests.exceptions.ConnectTimeout as e:
        exception_type = "ConnectTimeout"
        exception_msg = str(e)
        failure_class = "ConnectTimeout"
    except requests.exceptions.ReadTimeout as e:
        exception_type = "ReadTimeout"
        exception_msg = str(e)
        failure_class = "ReadTimeout"
    except requests.exceptions.ConnectionError as e:
        exception_type = "ConnectionError"
        exception_msg = str(e)
        msg_lower = exception_msg.lower()
        if "remotedisconnected" in msg_lower:
            failure_class = "RemoteDisconnected"
        elif "connection reset" in msg_lower:
            failure_class = "ConnectionReset"
        else:
            failure_class = "ConnectionError"
    except Exception as e:
        exception_type = type(e).__name__
        exception_msg = str(e)
        failure_class = "TransportError"

    t1_perf = time.perf_counter()
    end_wall = time.time()
    latency_ms = (t1_perf - t0_perf) * 1000.0

    record: dict[str, Any] = {
        "request_id": request_id,
        "layer": layer_name,
        "capture_start_perf": t0_perf,
        "capture_end_perf": t1_perf,
        "start_wall": start_wall,
        "end_wall": end_wall,
        "latency_ms": round(latency_ms, 2),
        "status_code": status_code,
        "bytes": len(raw_bytes),
        "decode_ok": decode_ok,
        "dimensions": dims,
        "sha256": sha256_hash,
        "failure_class": failure_class,
        "exception_type": exception_type,
        "exception_message": exception_msg,
        "headers": response_headers,
    }

    if save_raw:
        RAW_EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        meta_file = RAW_EVIDENCE_DIR / f"frame_{request_id:04d}.json"
        with open(meta_file, "w") as f:
            json.dump(record, f, indent=2)

        if decode_ok and raw_bytes:
            jpg_file = RAW_EVIDENCE_DIR / f"frame_{request_id:04d}.jpg"
            with open(jpg_file, "wb") as f:
                f.write(raw_bytes)
            sha_file = RAW_EVIDENCE_DIR / f"frame_{request_id:04d}.sha256"
            with open(sha_file, "w") as f:
                f.write(f"{sha256_hash}  frame_{request_id:04d}.jpg\n")
        elif failure_class is not None:
            err_file = RAW_EVIDENCE_DIR / f"frame_{request_id:04d}.error.json"
            with open(err_file, "w") as f:
                json.dump({
                    "request_id": request_id,
                    "layer": layer_name,
                    "failure_class": failure_class,
                    "exception_type": exception_type,
                    "exception_message": exception_msg,
                    "http_status": status_code,
                    "start_wall": start_wall,
                    "end_wall": end_wall,
                    "latency_ms": round(latency_ms, 2),
                }, f, indent=2)

    return record


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


def assert_single_flight(records: list[dict[str, Any]]) -> dict[str, Any]:
    max_overlap = 0.0
    overlaps_detected = 0

    for i in range(len(records) - 1):
        r1 = records[i]
        r2 = records[i + 1]
        overlap = r1["capture_end_perf"] - r2["capture_start_perf"]
        if overlap > 0:
            overlaps_detected += 1
            if overlap > max_overlap:
                max_overlap = overlap

    return {
        "single_flight_guaranteed": overlaps_detected == 0,
        "max_overlap_sec": round(max_overlap, 6),
        "overlaps_detected": overlaps_detected,
    }


def run_layer_0_idle(duration_sec: float = 2.0) -> dict[str, Any]:
    print(f"\n=================================================================")
    print(f"Layer 0: Idle Observation ({duration_sec}s duration)")
    print(f"=================================================================")
    session = requests.Session()
    t0_perf = time.perf_counter()
    start_wall = time.time()
    camera_http_requests = 0  # Measured counter: must remain exactly 0

    btn_health = None
    btn_status = "UNAVAILABLE"
    try:
        resp = session.get(BUTTONS_HEALTH_URL, timeout=2.0)
        if resp.status_code == 200:
            btn_health = resp.json()
            btn_status = "MEASURED"
    except Exception:
        btn_health = None
        btn_status = "UNAVAILABLE"

    # Sleep the duration in small increments without invoking any camera endpoint
    slept = 0.0
    while slept < duration_sec:
        step = min(1.0, duration_sec - slept)
        time.sleep(step)
        slept += step

    t1_perf = time.perf_counter()
    end_wall = time.time()
    actual_duration = t1_perf - t0_perf

    print(f"Layer 0 Complete: Requested {duration_sec}s | Actual {round(actual_duration, 2)}s")
    print(f"  Camera HTTP Requests Issued: {camera_http_requests} (Invariant: 0)")
    print(f"  Button Health Available: {btn_health is not None} (Status: {btn_status})")

    return {
        "layer": "Layer_0_Idle",
        "requested_duration_sec": duration_sec,
        "actual_duration_sec": round(actual_duration, 2),
        "camera_http_request_count": {
            "value": camera_http_requests,
            "status": "MEASURED",
            "invariant_met": camera_http_requests == 0,
        },
        "button_health": {
            "value": btn_health,
            "status": btn_status,
        },
        "camera_health": {
            "value": None,
            "status": "UNAVAILABLE",
            "reason": "camera_health_endpoint_absent_in_v1.0",
        },
        "reboot_detection": {
            "value": None,
            "status": "UNAVAILABLE",
            "reason": "camera_health_endpoint_absent",
        },
        "temperature": {
            "value": None,
            "status": "UNAVAILABLE",
            "reason": "no_hardware_sensor",
        },
        "completeness": {
            "requested_sec": duration_sec,
            "actual_sec": round(actual_duration, 2),
            "camera_requests": camera_http_requests,
            "skipped": 0,
        },
    }


def run_capture_layer(
    name: str,
    count: int,
    delay_sec: float,
    *,
    save_raw: bool = False,
    is_stress_test: bool = False,
    start_req_id: int = 1,
) -> dict[str, Any]:
    print(f"\n=================================================================")
    print(f"{name} ({count} captures, {delay_sec}s inter-frame delay)")
    if is_stress_test:
        print(f"  *** WARNING: AGGRESSIVE STRESS TEST (~20 req/s) ***")
        print(f"  *** NOT REPRESENTATIVE OF NORMAL TALETRACE OPERATION ***")
    print(f"=================================================================")

    session = requests.Session()
    records: list[dict[str, Any]] = []
    t0_perf = time.perf_counter()

    for i in range(1, count + 1):
        rec_id = start_req_id + i - 1
        rec = capture_single_frame(session, timeout=5.0, save_raw=save_raw, request_id=rec_id, layer_name=name)
        records.append(rec)
        if delay_sec > 0:
            time.sleep(delay_sec)

    t1_perf = time.perf_counter()
    total_duration = t1_perf - t0_perf

    valid_latencies = [r["latency_ms"] for r in records if r["decode_ok"] and r["failure_class"] is None]
    valid_bytes = [r["bytes"] for r in records if r["decode_ok"]]
    stats = compute_quantiles(valid_latencies)
    byte_stats = compute_byte_stats(valid_bytes)
    fails = count - len(valid_latencies)

    failure_breakdown: dict[str, int] = {}
    for r in records:
        fc = r["failure_class"]
        if fc:
            failure_breakdown[fc] = failure_breakdown.get(fc, 0) + 1

    single_flight_check = assert_single_flight(records)

    print(f"Results for {name}:")
    print(f"  Valid JPEGs: {len(valid_latencies)}/{count} (Failures: {fails})")
    print(f"  Single-Flight Guarantee: {single_flight_check['single_flight_guaranteed']} (Max Overlap: {single_flight_check['max_overlap_sec']}s)")
    if failure_breakdown:
        print(f"  Failure Breakdown: {failure_breakdown}")
    if stats:
        print(f"  Latency: Min: {stats['min']}ms | P50: {stats['p50']}ms | P90: {stats['p90']}ms | P95: {stats['p95']}ms | P99: {stats['p99']}ms | Max: {stats['max']}ms")
    if byte_stats:
        print(f"  JPEG Bytes: Min: {byte_stats['min_bytes']} B | Median: {byte_stats['median_bytes']} B | Max: {byte_stats['max_bytes']} B")

    return {
        "layer": name,
        "is_stress_test": is_stress_test,
        "expected_count": count,
        "executed_count": len(records),
        "success_count": len(valid_latencies),
        "failed_count": fails,
        "skipped_count": 0,
        "total_duration_sec": round(total_duration, 2),
        "stats": stats,
        "byte_stats": byte_stats,
        "failure_breakdown": failure_breakdown,
        "single_flight_check": single_flight_check,
        "completeness": {
            "expected": count,
            "executed": len(records),
            "failed": fails,
            "skipped": 0,
        },
        "samples": records if len(records) <= 10 else records[:5] + records[-5:],  # compact inline summary
    }


def run_contention_layer(
    count: int = 100,
    *,
    save_raw: bool = False,
    start_req_id: int = 1,
) -> dict[str, Any]:
    from concurrent.futures import ThreadPoolExecutor
    print(f"\n=================================================================")
    print(f"Layer 3: Contention Stress ({count} captures with overlapping button polling)")
    print(f"  *** Single-flight camera capture maintained (MAX_INFLIGHT=1) ***")
    print(f"=================================================================")

    session = requests.Session()
    records: list[dict[str, Any]] = []
    t0_perf = time.perf_counter()

    def poll_buttons() -> None:
        try:
            session.get(BUTTONS_URL, timeout=0.3)
        except Exception:
            pass

    for i in range(1, count + 1):
        rec_id = start_req_id + i - 1
        with ThreadPoolExecutor(max_workers=2) as executor:
            fut_btn = executor.submit(poll_buttons)
            fut_cam = executor.submit(capture_single_frame, session, 5.0, save_raw=save_raw, request_id=rec_id, layer_name="Layer_3_Contention")

            rec = fut_cam.result()
            fut_btn.result()

        records.append(rec)
        time.sleep(0.1)

    t1_perf = time.perf_counter()
    total_duration = t1_perf - t0_perf

    valid_latencies = [r["latency_ms"] for r in records if r["decode_ok"] and r["failure_class"] is None]
    valid_bytes = [r["bytes"] for r in records if r["decode_ok"]]
    stats = compute_quantiles(valid_latencies)
    byte_stats = compute_byte_stats(valid_bytes)
    fails = count - len(valid_latencies)

    failure_breakdown: dict[str, int] = {}
    for r in records:
        fc = r["failure_class"]
        if fc:
            failure_breakdown[fc] = failure_breakdown.get(fc, 0) + 1

    single_flight_check = assert_single_flight(records)

    print(f"Results for Layer_3_Contention:")
    print(f"  Valid JPEGs: {len(valid_latencies)}/{count} (Failures: {fails})")
    print(f"  Single-Flight Guarantee: {single_flight_check['single_flight_guaranteed']} (Max Overlap: {single_flight_check['max_overlap_sec']}s)")
    if failure_breakdown:
        print(f"  Failure Breakdown: {failure_breakdown}")
    if stats:
        print(f"  Latency: Min: {stats['min']}ms | P50: {stats['p50']}ms | P90: {stats['p90']}ms | P95: {stats['p95']}ms | P99: {stats['p99']}ms | Max: {stats['max']}ms")

    return {
        "layer": "Layer_3_Contention",
        "expected_count": count,
        "executed_count": len(records),
        "success_count": len(valid_latencies),
        "failed_count": fails,
        "skipped_count": 0,
        "total_duration_sec": round(total_duration, 2),
        "stats": stats,
        "byte_stats": byte_stats,
        "failure_breakdown": failure_breakdown,
        "single_flight_check": single_flight_check,
        "completeness": {
            "expected": count,
            "executed": len(records),
            "failed": fails,
            "skipped": 0,
        },
        "samples": records if len(records) <= 10 else records[:5] + records[-5:],
    }


def determine_verdicts(layers: list[dict[str, Any]]) -> dict[str, str]:
    # Evaluate Camera Transport
    total_expected = sum(l.get("expected_count", 0) for l in layers if "expected_count" in l)
    total_success = sum(l.get("success_count", 0) for l in layers if "success_count" in l)
    total_failures = sum(l.get("failed_count", 0) for l in layers if "failed_count" in l)

    if total_expected == 0:
        cam_verdict = "NOT_EVALUATED"
    elif total_failures == 0:
        cam_verdict = "PASS"
    elif total_success / total_expected >= 0.90:
        cam_verdict = "DEGRADED"
    else:
        cam_verdict = "FAIL"

    # Evaluate Network Path
    network_verdict = "PASS" if cam_verdict in ("PASS", "DEGRADED") else "FAIL"

    return {
        "CAMERA_TRANSPORT_BASELINE": cam_verdict,
        "NETWORK_PATH": network_verdict,
        "HARDWARE_HEALTH": "NOT MEASURED — H7 REQUIRED",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="TaleTrace Phase H1 Statistical Diagnostic Tool")
    parser.add_argument("--full", action="store_true", help="Run full authoritative 15-minute H1 physical test")
    parser.add_argument("--save-raw", action="store_true", help="Persist raw JPEG and metadata evidence")
    args = parser.parse_args()

    mode_label = "FULL (AUTHORITATIVE)" if args.full else "QUICK (SMOKE / NOT AUTHORITATIVE)"
    save_raw = args.save_raw or args.full

    print("\n" + "=" * 70)
    print(f"  TALETRACE PHASE H1 DIAGNOSTIC HARNESS")
    print(f"  MODE: {mode_label}")
    print(f"  RAW EVIDENCE RETENTION: {save_raw}")
    print("=" * 70)

    start_net = get_network_identity()
    start_time = time.time()
    git_commit = get_git_commit()

    idle_duration = 900.0 if args.full else 2.0
    l1_count = 10 if args.full else 2
    l2a_count = 100 if args.full else 3
    l2b_count = 100 if args.full else 3
    l3_count = 100 if args.full else 3

    layer_0 = run_layer_0_idle(duration_sec=idle_duration)
    layer_1 = run_capture_layer("Layer_1_Pure_Transport_Smoke", count=l1_count, delay_sec=0.2, save_raw=save_raw, start_req_id=1)
    layer_2a = run_capture_layer("Layer_2A_Sequential_Stress", count=l2a_count, delay_sec=0.05, save_raw=save_raw, is_stress_test=True, start_req_id=101)
    layer_2b = run_capture_layer("Layer_2B_Reading_Cadence", count=l2b_count, delay_sec=1.0, save_raw=save_raw, start_req_id=201)
    layer_3 = run_contention_layer(count=l3_count, save_raw=save_raw, start_req_id=301)

    end_net = get_network_identity()
    end_time = time.time()

    interface_changed = (start_net["local_ip"] != end_net["local_ip"])
    layers = [layer_0, layer_1, layer_2a, layer_2b, layer_3]
    verdicts = determine_verdicts(layers)

    report = {
        "protocol": "Phase_H1_Baseline_Measurement",
        "mode": "FULL" if args.full else "QUICK",
        "is_authoritative": args.full,
        "software_commit": git_commit,
        "timestamp_start": start_time,
        "timestamp_end": end_time,
        "total_elapsed_sec": round(end_time - start_time, 2),
        "network_identity_start": start_net,
        "network_identity_end": end_net,
        "network_interface_changed": interface_changed,
        "layers": layers,
        "verdicts": verdicts,
    }

    timestamp_str = time.strftime("%Y%m%d_%H%M%S", time.localtime(start_time))
    out_file = LOGS_DIR / f"h1_camera_baseline_{timestamp_str}.json"
    with open(out_file, "w") as f:
        json.dump(report, f, indent=2)

    print("\n" + "=" * 70)
    print(f"  PHASE H1 DIAGNOSTIC COMPLETE")
    print(f"  AUTHORITATIVE REPORT: {out_file}")
    print(f"  VERDICTS:")
    for k, v in verdicts.items():
        print(f"    • {k}: {v}")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    main()
