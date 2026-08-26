"""Authentication routes and dependency."""

import logging
import secrets
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from pwdlib import PasswordHash
from sqlalchemy.orm import Session as OrmSession

from backend.app.modules.database.models import Reader, new_id
from backend.app.modules.database.session import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])
pwd_context = PasswordHash.recommended()

COOKIE_NAME = "taletrace_session"


def get_current_reader(request: Request, db: OrmSession = Depends(get_db)) -> Reader:
    """FastAPI dependency to authenticate requests using the HTTP-only cookie."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
        
    reader = db.query(Reader).filter(Reader.session_token == token).first()
    if not reader:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session")
        
    return reader

class SignupRequest(BaseModel):
    name: str = Field(min_length=1)
    email: str = Field(min_length=5)
    password: str = Field(min_length=8)

class LoginRequest(BaseModel):
    email: str
    password: str


def _set_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        secure=False,
        samesite="lax",
    )


@router.post("/signup")
def signup(body: SignupRequest, response: Response, db: OrmSession = Depends(get_db)) -> dict[str, Any]:
    existing = db.query(Reader).filter(Reader.email == body.email).first()
    if existing:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Email already registered")
        
    session_token = secrets.token_hex(32)
    reader = Reader(
        id=new_id(),
        name=body.name,
        email=body.email,
        password_hash=pwd_context.hash(body.password),
        session_token=session_token,
    )
    db.add(reader)
    db.commit()
    
    _set_cookie(response, session_token)
    return {"id": reader.id, "name": reader.name, "email": reader.email}


@router.post("/login")
def login(body: LoginRequest, response: Response, db: OrmSession = Depends(get_db)) -> dict[str, Any]:
    reader = db.query(Reader).filter(Reader.email == body.email).first()
    if not reader or not reader.password_hash or not pwd_context.verify(body.password, reader.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
        
    # Rotate token
    session_token = secrets.token_hex(32)
    reader.session_token = session_token
    db.commit()
    
    _set_cookie(response, session_token)
    return {"id": reader.id, "name": reader.name, "email": reader.email}


@router.post("/logout")
def logout(response: Response, reader: Reader = Depends(get_current_reader), db: OrmSession = Depends(get_db)) -> dict[str, Any]:
    reader.session_token = None
    db.commit()
    response.delete_cookie(key=COOKIE_NAME, httponly=True, secure=False, samesite="lax")
    return {"success": True}
