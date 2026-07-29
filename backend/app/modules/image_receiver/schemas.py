"""Response models for the single-image testing endpoint."""

from pydantic import BaseModel


class UploadFrameResponse(BaseModel):
    status: str = "success"
    text: str
    latest_image: str
    processed_image: str
    output_file: str