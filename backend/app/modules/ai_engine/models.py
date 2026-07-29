"""Provider-neutral AI Engine request and placeholder response models."""

from pydantic import BaseModel, Field


class AiInput(BaseModel):
    """Context supplied to a future AI capability."""

    content_reference: str | None = None
    text: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class AiPlaceholderResponse(BaseModel):
    """Stable response returned until a capability is implemented."""

    status: str = "pending"
    capability: str = "ai_engine"
    message: str = "AI capability placeholder"


class AiExplainRequest(AiInput):
    """Input contract for the existing explanation route."""


class AiExplainResponse(AiPlaceholderResponse):
    """Placeholder response for the existing explanation route."""
