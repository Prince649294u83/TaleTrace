"""Provider-neutral OCR ports; no provider calls are made here.

`OcrProvider` is the seam that keeps provider choice invisible. Google Vision
wants base64 over HTTPS, Paddle wants a BGR array in-process, and the JSON
provider wants a file path — but all three return `list[RecognizedWord]`, so
nothing downstream can tell them apart. A caller that needed to know which one
ran would be a caller that had to change when the provider did.

`accepts` is part of the port rather than tribal knowledge because the three
disagree about their input. It lets the pipeline hand each provider what it can
actually take, instead of every call site remembering that Paddle cannot be given
a URL.
"""

from typing import Any, Protocol, runtime_checkable

from backend.app.modules.ocr.models import (
    OcrProcessRequest,
    OcrProcessResponse,
    RecognizedWord,
)


class OcrParserInterface(Protocol):
    """Translate a provider response into the shared OCR response contract."""

    def parse(self, provider_response: object) -> OcrProcessResponse:
        """Parse an external OCR response."""
        ...


class OcrProcessorInterface(Protocol):
    """Submit processed image input to an OCR provider adapter."""

    def process(self, request: OcrProcessRequest) -> OcrProcessResponse:
        """Process an OCR request through a provider implementation."""
        ...


@runtime_checkable
class OcrProvider(Protocol):
    """One OCR engine, reduced to the only thing callers need from it.

    Implementations raise on transport and credential failures rather than
    returning an empty list. An empty page and a failed request are different
    facts: the first should advance the session, the second should be retried,
    and a provider that conflates them makes an outage look like a blank page.
    """

    provider_name: str

    def extract(self, source: Any) -> list[RecognizedWord]:
        """Recognise words in `source`, in reading order where the engine knows it."""
        ...

    def accepts(self, source: Any) -> bool:
        """Whether this provider can take `source` at all.

        Checked before `extract` so an unsupported input is a routing decision
        rather than an exception from inside a provider.
        """
        ...