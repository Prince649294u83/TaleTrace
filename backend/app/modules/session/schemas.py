from pydantic import BaseModel

class SessionStartRequest(BaseModel):
    client_reference: str | None = None

class SessionEndRequest(BaseModel):
    session_reference: str | None = None

class CurrentSessionRequest(BaseModel):
    session_reference: str | None = None

class SessionResponse(BaseModel):
    status: str