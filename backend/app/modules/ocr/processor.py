"""Synchronous Google Cloud Vision adapter for one processed image."""

import base64

import requests


class OcrConfigurationError(RuntimeError):
    """Raised when OCR credentials have not been configured."""


class OcrProviderError(RuntimeError):
    """Raised when Google Cloud Vision rejects or cannot process the image."""


class GoogleVisionOcrProcessor:
    """Call DOCUMENT_TEXT_DETECTION once, without retries."""

    endpoint = "https://vision.googleapis.com/v1/images:annotate"

    def __init__(self, api_key: str | None, timeout_seconds: float = 60.0) -> None:
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def extract_text(self, image_bytes: bytes) -> str:
        if not self.api_key:
            raise OcrConfigurationError(
                "GOOGLE_CLOUD_VISION_API_KEY is not configured"
            )

        payload = {
            "requests": [{
                "image": {"content": base64.b64encode(image_bytes).decode("ascii")},
                "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
            }]
        }
        try:
            response = requests.post(
                self.endpoint,
                params={"key": self.api_key},
                json=payload,
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise OcrProviderError(f"Google Cloud Vision request failed: {exc}") from exc

        if not response.ok:
            raise OcrProviderError(
                f"Google Cloud Vision returned HTTP {response.status_code}: "
                f"{response.text[:500]}"
            )
        try:
            result = response.json()["responses"][0]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise OcrProviderError("Google Cloud Vision returned an invalid response") from exc

        if error := result.get("error"):
            raise OcrProviderError(
                f"Google Cloud Vision OCR failed: {error.get('message', 'unknown provider error')}"
            )
        annotation = result.get("fullTextAnnotation")
        if not annotation or "text" not in annotation:
            raise OcrProviderError("Google Cloud Vision found no document text")
        return str(annotation["text"])