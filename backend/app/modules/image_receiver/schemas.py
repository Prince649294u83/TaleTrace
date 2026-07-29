from pydantic import BaseModel

class UploadFrameRequest(BaseModel):
    frame_reference: str | None = None

class UploadFrameResponse(BaseModel):
    status: str