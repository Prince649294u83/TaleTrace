"""Synchronous Google Cloud Vision adapter for one processed image."""

from google.api_core import exceptions as google_exceptions
from google.auth.exceptions import DefaultCredentialsError, GoogleAuthError
from google.cloud import vision


class OcrConfigurationError(RuntimeError):
    """Raised when OCR credentials have not been configured."""


class OcrProviderError(RuntimeError):
    """Raised when Google Cloud Vision rejects or cannot process the image."""


class OcrTimeoutError(OcrProviderError):
    """Raised when Google Cloud Vision does not respond before the deadline."""


class GoogleVisionOcrProcessor:
    """Call DOCUMENT_TEXT_DETECTION once using externally configured ADC."""

    def __init__(self, timeout_seconds: float = 60.0) -> None:
        self.timeout_seconds = timeout_seconds

    def extract_text(self, image_bytes: bytes) -> str:
        try:
            client = vision.ImageAnnotatorClient()
            response = client.document_text_detection(
                image=vision.Image(content=image_bytes),
                timeout=self.timeout_seconds,
            )
        except DefaultCredentialsError as exc:
            raise OcrConfigurationError(
                "Google Application Default Credentials are not configured"
            ) from exc
        except GoogleAuthError as exc:
            raise OcrConfigurationError(
                f"Google authentication failed: {exc}"
            ) from exc
        except (google_exceptions.DeadlineExceeded, google_exceptions.ServiceUnavailable) as exc:
            raise OcrTimeoutError(f"Google Cloud Vision request timed out: {exc}") from exc
        except google_exceptions.GoogleAPICallError as exc:
            raise OcrProviderError(f"Google Cloud Vision request failed: {exc}") from exc
        except OSError as exc:
            raise OcrConfigurationError(f"Google credentials could not be loaded: {exc}") from exc

        if response.error.message:
            raise OcrProviderError(
                f"Google Cloud Vision OCR failed: {response.error.message}"
            )
        return response.full_text_annotation.text
