"""Common response envelope shared by API modules."""

from typing import Generic, TypeVar

from pydantic import BaseModel, Field

PayloadT = TypeVar("PayloadT")


class ResponseEnvelope(BaseModel, Generic[PayloadT]):
    """Stable response shape for successful and placeholder API responses."""

    success: bool = True
    message: str = "Request completed"
    data: PayloadT | None = None
    errors: list[str] = Field(default_factory=list)