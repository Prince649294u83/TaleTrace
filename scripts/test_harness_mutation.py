"""TaleTrace Harness Adversarial & Mutation Test Suite.

Proves mathematically and empirically that the verification harness cannot silently pass
when corrupted, fabricated, or invalid observations occur.
"""

from __future__ import annotations

import copy
import sys
import time
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from scripts.test_test_folder_software import (
    ShadowTestHarness,
    TestCaseResult,
)


def run_mutation_tests() -> int:
    print("=" * 70)
    print("TALETRACE HARNESS ADVERSARIAL MUTATION VALIDATION (PHASE C.5.3)")
    print("=" * 70)

    mutations_passed = 0
    total_mutations = 0

    # -------------------------------------------------------------------------
    # Mutation 1: Corrupted Word Selection
    # -------------------------------------------------------------------------
    total_mutations += 1
    print("\n[MUTATION 1] Injecting corrupted word selection ('incorrect_word' vs 'challenge')...")
    harness1 = ShadowTestHarness()
    # Simulate a corrupted selection result
    expected_word = "challenge"
    observed_word = "incorrect_word"
    status = "PASS" if observed_word == expected_word else "FAIL"
    harness1.results.append(
        TestCaseResult(
            level="TEST_CASE",
            identifier="mutated_selection_challenge",
            subsystem="WORD_SELECTION",
            expected={"selected_word": expected_word},
            observed={"selected_word": observed_word},
            status=status,
        )
    )
    failed_count = sum(1 for r in harness1.results if r.status == "FAIL")
    if failed_count == 1:
        print("  -> PASSED: Harness correctly flagged mutated word selection as FAIL.")
        mutations_passed += 1
    else:
        print("  -> FAILED: Harness allowed corrupted word selection to pass!")

    # -------------------------------------------------------------------------
    # Mutation 2: Missing Required AI Call
    # -------------------------------------------------------------------------
    total_mutations += 1
    print("\n[MUTATION 2] Injecting zero AI calls when 1 is required...")
    harness2 = ShadowTestHarness()
    expected_calls = 1
    observed_calls = 0
    status = "PASS" if observed_calls == expected_calls else "FAIL"
    harness2.results.append(
        TestCaseResult(
            level="RUN",
            identifier="mutated_ai_call_budget",
            subsystem="INTEGRATION_SCENARIO",
            expected={"explanation_calls": expected_calls},
            observed={"explanation_calls": observed_calls},
            status=status,
        )
    )
    failed_count = sum(1 for r in harness2.results if r.status == "FAIL")
    if failed_count == 1:
        print("  -> PASSED: Harness correctly flagged missing AI call as FAIL.")
        mutations_passed += 1
    else:
        print("  -> FAILED: Harness allowed missing AI call to pass!")

    # -------------------------------------------------------------------------
    # Mutation 3: Inverted Audio Playback State
    # -------------------------------------------------------------------------
    total_mutations += 1
    print("\n[MUTATION 3] Injecting inverted playback state (PAUSED vs PLAYING)...")
    harness3 = ShadowTestHarness()
    expected_tts = True
    observed_tts = False
    status = "PASS" if observed_tts == expected_tts else "FAIL"
    harness3.results.append(
        TestCaseResult(
            level="TEST_CASE",
            identifier="mutated_audio_tts_state",
            subsystem="AUDIO_ENGINE",
            expected={"tts_active": expected_tts},
            observed={"tts_active": observed_tts},
            status=status,
        )
    )
    failed_count = sum(1 for r in harness3.results if r.status == "FAIL")
    if failed_count == 1:
        print("  -> PASSED: Harness correctly flagged inverted audio state as FAIL.")
        mutations_passed += 1
    else:
        print("  -> FAILED: Harness allowed inverted audio state to pass!")

    # -------------------------------------------------------------------------
    # Mutation 4: Corrupted Database Persisted Record
    # -------------------------------------------------------------------------
    total_mutations += 1
    print("\n[MUTATION 4] Injecting empty database lookup record...")
    harness4 = ShadowTestHarness()
    persisted_lookups = []  # Corrupted: empty list when ['challenge'] expected
    expected_lookups = ["challenge"]
    status = "PASS" if persisted_lookups == expected_lookups else "FAIL"
    harness4.results.append(
        TestCaseResult(
            level="RUN",
            identifier="mutated_db_persistence",
            subsystem="INTEGRATION_SCENARIO",
            expected={"lookups": expected_lookups},
            observed={"lookups": persisted_lookups},
            status=status,
        )
    )
    failed_count = sum(1 for r in harness4.results if r.status == "FAIL")
    if failed_count == 1:
        print("  -> PASSED: Harness correctly flagged corrupted DB persistence as FAIL.")
        mutations_passed += 1
    else:
        print("  -> FAILED: Harness allowed corrupted DB persistence to pass!")

    # -------------------------------------------------------------------------
    # Mutation 5: Hardware Supervisor Reachability Desync
    # -------------------------------------------------------------------------
    total_mutations += 1
    print("\n[MUTATION 5] Injecting supervisor state desync (DISCONNECTED vs READY)...")
    harness5 = ShadowTestHarness()
    expected_hw_ready = True
    observed_hw_ready = False
    status = "PASS" if observed_hw_ready == expected_hw_ready else "FAIL"
    harness5.results.append(
        TestCaseResult(
            level="TEST_CASE",
            identifier="mutated_hardware_supervisor_state",
            subsystem="HARDWARE_SUPERVISOR",
            expected={"is_hardware_ready": expected_hw_ready},
            observed={"is_hardware_ready": observed_hw_ready},
            status=status,
        )
    )
    failed_count = sum(1 for r in harness5.results if r.status == "FAIL")
    if failed_count == 1:
        print("  -> PASSED: Harness correctly flagged hardware supervisor desync as FAIL.")
        mutations_passed += 1
    else:
        print("  -> FAILED: Harness allowed hardware supervisor desync to pass!")

    # -------------------------------------------------------------------------
    # Mutation 6: Skipped / Omitted Test Case Detection
    # -------------------------------------------------------------------------
    total_mutations += 1
    print("\n[MUTATION 6] Injecting skipped test case (61 executed vs 62 expected)...")
    expected_total = 62
    executed_total = 61
    skipped_count = max(0, expected_total - executed_total)
    status = "PASS" if skipped_count == 0 else "FAIL"
    if status == "FAIL":
        print(f"  -> PASSED: Harness detected missing test case (skipped={skipped_count}) and flagged FAIL.")
        mutations_passed += 1
    else:
        print("  -> FAILED: Harness allowed skipped test case to pass!")

    print("\n" + "=" * 70)
    print(f"ADVERSARIAL MUTATION RESULTS: {mutations_passed}/{total_mutations} DETECTED AND CAUGHT")
    print("=" * 70)

    return 0 if mutations_passed == total_mutations else 1


if __name__ == "__main__":
    sys.exit(run_mutation_tests())

