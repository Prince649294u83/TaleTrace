"""Tests for Phase C/D/E/G: Dual-Identity Spatial Modeling, 3-Tier Fusion, Tracker, Consensus, Transactions, and HardwareSupervisor."""

from __future__ import annotations

import time
import numpy as np
import pytest

from backend.app.modules.gesture_engine.consensus import GestureConsensus
from backend.app.modules.gesture_engine.fusion import ObservationFuser
from backend.app.modules.gesture_engine.selection_models import (
    CoordinateSpace,
    DetectionObservation,
    FingerObservation,
    FingerPoint,
    FrameContext,
    PageContext,
    SelectionConfig,
    SelectionResult,
    SelectionStatus,
)
from backend.app.modules.gesture_engine.tracker import GestureMotionTracker
from backend.app.modules.gesture_engine.transaction import GestureTransaction, TransactionStatus
from backend.app.modules.image_receiver.hardware_supervisor import HardwareSupervisor
from backend.app.modules.image_receiver.types import DeviceHealthState, DisplayState
from backend.app.modules.ocr.geometry_validator import PageGeometryValidator
from backend.app.modules.ocr.models import RecognizedWord
from backend.app.shared.events import SessionEvent


class TestSpatialModels:
    def test_coordinate_space_and_page_context(self):
        space = CoordinateSpace(width=1024, height=768, crop=(0, 0, 1024, 768), rotation=0, mirror=False)
        word = RecognizedWord(
            text="TaleTrace",
            bbox=(100, 100, 200, 130),
            center_x=150.0,
            center_y=115.0,
            confidence=0.99,
            word_index=0,
            line_index=0,
            paragraph_index=0,
        )
        ctx = PageContext(
            page_id="page_001",
            page_version=1,
            coordinate_space=space,
            ocr_words=(word,),
        )
        assert ctx.page_id == "page_001"
        assert len(ctx.ocr_words) == 1
        assert ctx.ocr_words[0].text == "TaleTrace"

    def test_page_geometry_validator_fingerprint(self):
        img = np.zeros((400, 400, 3), dtype=np.uint8)
        img[50:150, 50:150] = 255  # Distinct background feature
        fp1 = PageGeometryValidator.compute_page_fingerprint(img, page_region=(0, 0, 400, 400))
        assert fp1 != ""

        # Masking dynamic gesture region preserves background fingerprint
        img2 = img.copy()
        img2[300:380, 300:380] = 180  # Moving finger in gesture region
        fp2 = PageGeometryValidator.compute_page_fingerprint(
            img2,
            page_region=(0, 0, 400, 400),
            gesture_region=(300, 300, 380, 380),
        )
        assert fp2 != ""


class TestObservationFusionAndTracker:
    def test_tracker_velocity_and_coasting(self):
        tracker = GestureMotionTracker()
        obs1 = DetectionObservation(
            detector_name="mediapipe",
            x=100.0,
            y=100.0,
            direction=(0.0, -1.0),
            confidence=0.8,
            timestamp=1000.0,
        )
        t_obs1 = tracker.update(obs1, timestamp=1000.0)
        assert t_obs1 is not None
        assert t_obs1.x == 100.0

        obs2 = DetectionObservation(
            detector_name="mediapipe",
            x=120.0,
            y=100.0,
            direction=(0.0, -1.0),
            confidence=0.8,
            timestamp=1000.1,
        )
        t_obs2 = tracker.update(obs2, timestamp=1000.1)
        assert t_obs2 is not None
        assert t_obs2.x > 100.0

        # Coasting frame when observation is missing
        t_obs3 = tracker.update(None, timestamp=1000.2)
        assert t_obs3 is not None
        assert t_obs3.is_predicted is True
        assert t_obs3.confidence < 0.8

        # Coasting frame 2
        t_obs4 = tracker.update(None, timestamp=1000.3)
        assert t_obs4 is not None

        # Coasting frame 3 exceeds max_coast (2) -> None
        t_obs5 = tracker.update(None, timestamp=1000.4)
        assert t_obs5 is None

    def test_observation_fusion_spatial_and_angular_agreement(self):
        fuser = ObservationFuser()
        mp_obs = DetectionObservation(
            detector_name="mediapipe",
            x=150.0,
            y=200.0,
            direction=(0.0, -1.0),
            confidence=0.7,
        )
        part_obs = DetectionObservation(
            detector_name="partial_finger",
            x=152.0,
            y=201.0,
            direction=(0.0, -1.0),
            confidence=0.6,
        )
        fused = fuser.fuse([mp_obs, part_obs], image_dims=(1024, 768))
        assert fused is not None
        assert fused.provenance == "fused"
        assert fused.confidence > 0.7  # Boosted


class TestGestureConsensusAndTransactions:
    def test_consensus_resolves_winner_and_margin(self):
        consensus = GestureConsensus()
        res_a = SelectionResult(
            status=SelectionStatus.SUCCESS,
            selected_word="read",
            word_index=0,
            confidence=0.85,
        )
        res_b = SelectionResult(
            status=SelectionStatus.SUCCESS,
            selected_word="books",
            word_index=1,
            confidence=0.50,
        )
        burst = [res_a, res_a, res_a, res_b, res_a]  # 4/5 for 'read'
        resolved = consensus.resolve(burst)
        assert resolved.status is SelectionStatus.SUCCESS
        assert resolved.selected_word == "read"

    def test_consensus_rejects_ambiguous_burst(self):
        consensus = GestureConsensus()
        res_a = SelectionResult(
            status=SelectionStatus.SUCCESS,
            selected_word="read",
            word_index=0,
            confidence=0.50,
        )
        res_b = SelectionResult(
            status=SelectionStatus.SUCCESS,
            selected_word="books",
            word_index=1,
            confidence=0.50,
        )
        burst = [res_a, res_b, res_a, res_b, None]  # 2 vs 2 tie without margin
        resolved = consensus.resolve(burst)
        assert resolved.status is SelectionStatus.LOW_CONFIDENCE

    def test_transaction_lifecycle_and_cancellation(self):
        now = time.monotonic()
        tx = GestureTransaction(
            transaction_id="tx_001",
            session_id="session_test",
            page_id="page_1",
            started_at=now,
            deadline_monotonic=now + 100.0,
        )
        assert tx.is_valid("session_test", "page_1")
        assert tx.status == TransactionStatus.PENDING

        tx.cancel("button released early")
        assert not tx.is_valid("session_test", "page_1")
        assert tx.status == TransactionStatus.CANCELLED


class TestHardwareSupervisor:
    def test_hardware_supervisor_readiness_gate(self):
        events = []
        supervisor = HardwareSupervisor(event_callback=lambda ev: events.append(ev))

        assert supervisor.camera_state == DeviceHealthState.DISCONNECTED
        assert supervisor.buttons_state == DeviceHealthState.DISCONNECTED
        assert not supervisor.is_hardware_ready

        # Camera comes online
        supervisor.update_camera_status(True)
        assert supervisor.camera_state == DeviceHealthState.READY
        assert SessionEvent.CAMERA_ON in events
        assert not supervisor.is_hardware_ready  # Buttons not ready yet

        # Buttons come online
        supervisor.update_buttons_status(True)
        assert supervisor.buttons_state == DeviceHealthState.READY
        assert SessionEvent.BUTTON_DEVICE_ONLINE in events
        assert supervisor.is_hardware_ready  # Both ready!
