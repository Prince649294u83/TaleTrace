from pydantic import BaseModel

class OcrProcessRequest(BaseModel):
    frame_reference: str | None = None

class OcrProcessResponse(BaseModel):
    status: str