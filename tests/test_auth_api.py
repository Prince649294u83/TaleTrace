"""Auth API and cross-reader isolation tests."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

from backend.app.main import app
from backend.app.modules.database.base import Base
from backend.app.modules.database.session import get_db
from backend.app.modules.database.models import Reader


@pytest.fixture
def db_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    # Seed the live-reader for general use (e.g. rig)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as db:
        db.add(Reader(id="live-reader", device_prefs={}))
        db.commit()
    yield factory
    engine.dispose()


@pytest.fixture
def client(db_factory):
    def override():
        session = db_factory()
        try:
            yield session
            session.commit()
        finally:
            session.close()

    app.dependency_overrides[get_db] = override
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_signup_creates_account_and_sets_cookie(client, db_factory):
    res = client.post("/api/auth/signup", json={"name": "Test", "email": "test@app.com", "password": "password"})
    assert res.status_code == 200
    
    # Cookie should be set
    assert "taletrace_session" in res.cookies

    # Reader should exist in db
    with db_factory() as db:
        reader = db.query(Reader).filter_by(email="test@app.com").first()
        assert reader is not None


def test_login_sets_cookie(client, db_factory):
    client.post("/api/auth/signup", json={"name": "A", "email": "a@a.com", "password": "password"})
    
    # Login
    res = client.post("/api/auth/login", json={"email": "a@a.com", "password": "password"})
    assert res.status_code == 200
    assert "taletrace_session" in res.cookies


def test_logout_clears_cookie(client):
    client.post("/api/auth/signup", json={"name": "A", "email": "a@a.com", "password": "password"})
    res = client.post("/api/auth/logout")
    assert res.status_code == 200
    
    # Check that cookie is wiped by verifying the next request fails
    res_me = client.get("/api/me")
    assert res_me.status_code == 401


def test_unauthenticated_requests_are_rejected(client):
    res = client.get("/api/me")
    assert res.status_code == 401
    
    res = client.get("/api/sessions")
    assert res.status_code == 401


def test_cross_reader_isolation(client, db_factory):
    # 1. Signup reader A
    res_a = client.post("/api/auth/signup", json={"name": "A", "email": "a@a.com", "password": "password"})
    client_a = TestClient(app, cookies=res_a.cookies)
    
    # 2. Signup reader B
    res_b = client.post("/api/auth/signup", json={"name": "B", "email": "b@b.com", "password": "password"})
    client_b = TestClient(app, cookies=res_b.cookies)
    
    # reader A creates a folder
    res = client_a.post("/api/folders", json={"name": "A-Folder"})
    assert res.status_code == 201
    folder_id_a = res.json()["id"]
    
    # reader A lists sessions — folders are embedded there
    data_a = client_a.get("/api/sessions").json()
    assert len(data_a["folders"]) == 1
    assert data_a["folders"][0]["name"] == "A-Folder"
    
    # reader B lists sessions — should see no folders
    data_b = client_b.get("/api/sessions").json()
    assert len(data_b["folders"]) == 0
