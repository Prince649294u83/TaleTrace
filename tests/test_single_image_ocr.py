"""Tests for the deliberately synchronous first OCR milestone."""

from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from google.api_core.exceptions import DeadlineExceeded
from google.auth.exceptions import DefaultCredentialsError

from backend.app.main import app
from backend.app.modules.image_receiver.router import provide_pipeline
from backend.app.modules.image_receiver.service import (
    ImageValidationError,
    OcrProcessorPort,
    PipelineBusyError,
    SingleImageOcrPipeline,
)
from backend.app.modules.ocr.processor import (
    GoogleVisionOcrProcessor,
    OcrConfigurationError,
    OcrProviderError,
    OcrTimeoutError,
)
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


def jpeg_bytes(width: int = 640, height: int = 480) -> bytes:
    encoded, image = cv2.imencode(
        ".jpg", np.full((height, width, 3), 255, dtype=np.uint8)
    )
    assert encoded
    return image.tobytes()


def make_pipeline(
    tmp_path: Path, ocr_processor: OcrProcessorPort
) -> SingleImageOcrPipeline:
    return SingleImageOcrPipeline(
        preprocessor=ImagePreprocessor(),
        ocr_processor=ocr_processor,
        latest_image_path=tmp_path / "latest.jpg",
        processed_image_path=tmp_path / "processed.jpg",
        output_path=tmp_path / "output.txt",
        max_image_bytes=1_000_000,
    )


def post_image(client: TestClient, content: bytes, content_type: str = "image/jpeg"):
    return client.post("/upload", files={"image": ("page.jpg", content, content_type)})


def test_upload_runs_full_transaction_and_overwrites_artifacts(tmp_path: Path) -> None:
    pipeline = make_pipeline(tmp_path, StubOcrProcessor())
    pipeline.output_path.write_text("old result", encoding="utf-8")
    app.dependency_overrides[provide_pipeline] = lambda: pipeline

    try:
        response = post_image(TestClient(app), jpeg_bytes())
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "status": "success",
        "processing": "completed",
        "characters": 16,
        "output_file": "backend/output/output.txt",
    }
    assert pipeline.latest_image_path.read_bytes().startswith(b"\xff\xd8")
    assert pipeline.processed_image_path.exists()
    output = pipeline.output_path.read_text(encoding="utf-8")
    assert "Image\nlatest.jpg\nTime\n" in output
    assert output.endswith("Extracted Text\nRecognized page\n")


def test_preprocessing_preserves_resolution(tmp_path: Path) -> None:
    destination = tmp_path / "processed.jpg"
    ImagePreprocessor().preprocess(jpeg_bytes(800, 600), destination)
    processed = cv2.imread(str(destination))
    assert processed.shape[:2] == (600, 800)


@pytest.mark.parametrize(
    ("content", "message"),
    [(b"", "empty"), (b"not-a-jpeg", "valid JPEG")],
)
def test_pipeline_rejects_empty_and_invalid_jpeg(
    tmp_path: Path, content: bytes, message: str
) -> None:
    with pytest.raises(ImageValidationError, match=message):
        make_pipeline(tmp_path, StubOcrProcessor()).process(content)


def test_pipeline_rejects_resolution_below_threshold(tmp_path: Path) -> None:
    with pytest.raises(ImageValidationError, match="at least 320x240"):
        make_pipeline(tmp_path, StubOcrProcessor()).process(jpeg_bytes(319, 240))


def test_busy_pipeline_rejects_instead_of_buffering(tmp_path: Path) -> None:
    pipeline = make_pipeline(tmp_path, StubOcrProcessor())
    pipeline._busy = True
    with pytest.raises(PipelineBusyError, match="already in progress"):
        pipeline.process(jpeg_bytes())


def test_ocr_failure_preserves_previous_output(tmp_path: Path) -> None:
    pipeline = make_pipeline(tmp_path, FailingOcrProcessor())
    pipeline.output_path.write_text("previous successful result", encoding="utf-8")
    with pytest.raises(OcrProviderError, match="provider failed once"):
        pipeline.process(jpeg_bytes())
    assert pipeline.output_path.read_text(encoding="utf-8") == "previous successful result"
    assert pipeline._busy is False


def test_upload_requires_an_image_and_jpeg_format(tmp_path: Path) -> None:
    app.dependency_overrides[provide_pipeline] = lambda: make_pipeline(
        tmp_path, StubOcrProcessor()
    )
    try:
        client = TestClient(app)
        missing = client.post("/upload")
        unsupported = post_image(client, b"png", "image/png")
    finally:
        app.dependency_overrides.clear()
    assert missing.status_code == 400
    assert unsupported.status_code == 415


def test_health_endpoints() -> None:
    client = TestClient(app)
    assert client.get("/").json() == "TaleTrace OCR Service Running"
    assert client.get("/health").json() == {"status": "healthy"}


def test_google_vision_uses_document_detection_and_full_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class Annotation:
        text = "Full page"

    class Error:
        message = ""

    class Response:
        full_text_annotation = Annotation()
        error = Error()

    class Client:
        def document_text_detection(self, **kwargs: object) -> Response:
            captured.update(kwargs)
            return Response()

    monkeypatch.setattr(
        "backend.app.modules.ocr.processor.vision.ImageAnnotatorClient", Client
    )
    text = GoogleVisionOcrProcessor(timeout_seconds=12).extract_text(b"processed")
    assert text == "Full page"
    assert captured["timeout"] == 12
    assert captured["image"].content == b"processed"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (DefaultCredentialsError("missing"), OcrConfigurationError),
        (DeadlineExceeded("slow"), OcrTimeoutError),
    ],
)
def test_google_vision_maps_authentication_and_timeout_errors(
    monkeypatch: pytest.MonkeyPatch, failure: Exception, expected: type[Exception]
) -> None:
    class Client:
        def __init__(self) -> None:
            raise failure

    monkeypatch.setattr(
        "backend.app.modules.ocr.processor.vision.ImageAnnotatorClient", Client
    )
    with pytest.raises(expected):
        GoogleVisionOcrProcessor().extract_text(b"processed")
