"""Provider-neutral OCR ports; no provider calls are made here."""

from typing import Protocol

from backend.app.modules.ocr.models import OcrProcessRequest, OcrProcessResponse


class OcrParserInterface(Protocol):
    """Translate a provider response into the shared OCR response contract."""

    def parse(self, provider_response: object) -> OcrProcessResponse:
        """Parse an external OCR response."""
        ...


class OcrProcessorInterface(Protocol):
    """Submit processed image input to a future OCR provider adapter."""

    def process(self, request: OcrProcessRequest) -> OcrProcessResponse:
        """Process an OCR request through a future provider implementation."""
        ...