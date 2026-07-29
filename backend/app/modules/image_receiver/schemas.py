"""Response models for the single-image testing endpoint."""

from pydantic import BaseModel


class UploadResponse(BaseModel):
    status: str = "success"
    processing: str = "completed"
    characters: int
    output_file: str


UploadFrameResponse = UploadResponse
