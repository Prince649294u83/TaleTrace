from pydantic import BaseModel

class AiExplainRequest(BaseModel):
    content_reference: str | None = None

class AiExplainResponse(BaseModel):
    status: str