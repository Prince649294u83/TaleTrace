"""Tests for the deliberately synchronous first OCR milestone."""

from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backend.app.main import app
from backend.app.modules.image_receiver.router import provide_pipeline
from backend.app.modules.image_receiver.service import (
    OcrProcessorPort,
    PipelineBusyError,
    SingleImageOcrPipeline,
)
from backend.app.modules.ocr.processor import GoogleVisionOcrProcessor, OcrProviderError
from backend.app.modules.preprocessing.processor import ImagePreprocessor


class StubOcrProcessor:
    def __init__(self, text: str = "Recognized page\n") -> None:
        self.text = text
        self.calls = 0

    def extract_text(self, image_bytes: bytes) -> str:
        assert image_bytes.startswith(b"\xff\xd8")
        self.calls += 1
        return self.text


class FailingOcrProcessor:
    def extract_text(self, image_bytes: bytes) -> str:
        raise OcrProviderError("provider failed once")


def jpeg_bytes() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (20, 20), "white").save(buffer, format="JPEG")
    return buffer.getvalue()


def make_pipeline(
    tmp_path: Path, ocr_processor: OcrProcessorPort
) -> SingleImageOcrPipeline:
    return SingleImageOcrPipeline(
        preprocessor=ImagePreprocessor(),
        ocr_processor=ocr_processor,
        latest_image_path=tmp_path / "latest.jpg",
        processed_image_path=tmp_path / "output" / "processed.jpg",
        output_path=tmp_path / "output" / "output.txt",
        max_image_bytes=1_000_000,
    )


def test_upload_runs_full_transaction_and_overwrites_text(tmp_path: Path) -> None:
    pipeline = make_pipeline(tmp_path, StubOcrProcessor())
    pipeline.output_path.parent.mkdir(parents=True)
    pipeline.output_path.write_text("old result", encoding="utf-8")
    app.dependency_overrides[provide_pipeline] = lambda: pipeline

    try:
        response = TestClient(app).post(
            "/upload_frame",
            content=jpeg_bytes(),
            headers={"Content-Type": "image/jpeg"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["text"] == "Recognized page\n"
    assert pipeline.latest_image_path.exists()
    assert pipeline.processed_image_path.exists()
    assert pipeline.output_path.read_text(encoding="utf-8") == "Recognized page\n"


def test_busy_pipeline_rejects_instead_of_buffering(tmp_path: Path) -> None:
    pipeline = make_pipeline(tmp_path, StubOcrProcessor())
    pipeline._busy = True

    with pytest.raises(PipelineBusyError, match="already in progress"):
        pipeline.process(jpeg_bytes())


def test_ocr_failure_stops_before_output_is_overwritten(tmp_path: Path) -> None:
    pipeline = make_pipeline(tmp_path, FailingOcrProcessor())
    pipeline.output_path.parent.mkdir(parents=True)
    pipeline.output_path.write_text("previous successful result", encoding="utf-8")

    with pytest.raises(OcrProviderError, match="provider failed once"):
        pipeline.process(jpeg_bytes())

    assert pipeline.output_path.read_text(encoding="utf-8") == "previous successful result"
    assert pipeline._busy is False


def test_upload_requires_jpeg_content_type(tmp_path: Path) -> None:
    pipeline = make_pipeline(tmp_path, StubOcrProcessor())
    app.dependency_overrides[provide_pipeline] = lambda: pipeline

    try:
        response = TestClient(app).post(
            "/upload_frame", content=jpeg_bytes(), headers={"Content-Type": "image/png"}
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 415


def test_google_vision_uses_document_detection_and_full_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class Response:
        ok = True

        def json(self) -> dict[str, object]:
            return {"responses": [{"fullTextAnnotation": {"text": "Full page"}}]}

    def fake_post(url: str, **kwargs: object) -> Response:
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("backend.app.modules.ocr.processor.requests.post", fake_post)

    text = GoogleVisionOcrProcessor("test-key").extract_text(b"processed-image")

    assert text == "Full page"
    assert captured["params"] == {"key": "test-key"}
    payload = captured["json"]
    assert isinstance(payload, dict)
    request = payload["requests"][0]  # type: ignore[index]
    assert request["features"] == [{"type": "DOCUMENT_TEXT_DETECTION"}]