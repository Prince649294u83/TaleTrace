from pydantic import BaseModel

class GestureSelectRequest(BaseModel):
    selection_reference: str | None = None

class GestureSelectResponse(BaseModel):
    status: str