"""Tests for the device diagnostic API endpoints."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from backend.app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_get_camera_frame_returns_jpeg(client):
    with patch("backend.app.api.device.Esp32Camera") as mock_cam_class:
        mock_cam = mock_cam_class.return_value
        mock_cam.configured = True
        mock_cam.frame.return_value = b"fakejpegbytes"
        
        response = client.get("/api/debug/camera")
        
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/jpeg"
        assert response.content == b"fakejpegbytes"
        mock_cam.frame.assert_called_once()


def test_get_camera_frame_unconfigured(client):
    with patch("backend.app.api.device.Esp32Camera") as mock_cam_class:
        mock_cam = mock_cam_class.return_value
        mock_cam.configured = False
        
        response = client.get("/api/debug/camera")
        
        assert response.status_code == 503
        assert "not configured" in response.json()["detail"].lower()
        mock_cam.frame.assert_not_called()


def test_get_camera_frame_timeout(client):
    with patch("backend.app.api.device.Esp32Camera") as mock_cam_class:
        mock_cam = mock_cam_class.return_value
        mock_cam.configured = True
        mock_cam.frame.return_value = None
        
        response = client.get("/api/debug/camera")
        
        assert response.status_code == 504
        assert "no frame" in response.json()["detail"].lower()
        mock_cam.frame.assert_called_once()
