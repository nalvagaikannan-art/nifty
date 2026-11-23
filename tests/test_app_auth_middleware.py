import base64

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.middleware.auth import AppAuthMiddleware


@pytest.fixture
def auth_app(monkeypatch):
    app = FastAPI()
    app.add_middleware(AppAuthMiddleware)

    @app.get("/private")
    async def private():
        return {"ok": True}

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    monkeypatch.setattr(settings, "env", "production")
    monkeypatch.setattr(settings, "app_auth_username", "qa-user")
    monkeypatch.setattr(settings, "app_auth_password", "qa-secret")
    return TestClient(app)


def test_auth_blocks_without_credentials(auth_app):
    response = auth_app.get("/private")
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Basic")


def test_auth_accepts_valid_credentials(auth_app):
    token = base64.b64encode(b"qa-user:qa-secret").decode()
    response = auth_app.get("/private", headers={"Authorization": f"Basic {token}"})
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_auth_rejects_wrong_password(auth_app):
    token = base64.b64encode(b"qa-user:wrong").decode()
    assert auth_app.get("/private", headers={"Authorization": f"Basic {token}"}).status_code == 401


def test_health_probe_is_public(auth_app):
    response = auth_app.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_production_fails_closed_without_auth_config(monkeypatch):
    app = FastAPI()
    app.add_middleware(AppAuthMiddleware)

    @app.get("/private")
    async def private():
        return {"ok": True}

    monkeypatch.setattr(settings, "env", "production")
    monkeypatch.setattr(settings, "app_auth_username", None)
    monkeypatch.setattr(settings, "app_auth_password", None)
    response = TestClient(app).get("/private")
    assert response.status_code == 503
