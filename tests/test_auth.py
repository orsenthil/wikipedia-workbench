import pytest
from fastapi.testclient import TestClient

from editor.app import app
from editor.auth import hash_password, verify_password


def test_hash_roundtrip():
    stored = hash_password("correct horse battery")
    assert verify_password("correct horse battery", stored)
    assert not verify_password("wrong", stored)
    assert not verify_password("x", "garbage")


@pytest.fixture
def secured(monkeypatch):
    monkeypatch.setenv("EDITOR_LOCAL", "0")
    monkeypatch.setenv("APP_PASSWORD_HASH", hash_password("correct horse battery"))
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    # Session cookies are https-only outside local mode.
    with TestClient(app, base_url="https://testserver") as client:
        yield client


def test_redirects_to_login_and_blocks_api(secured):
    resp = secured.get("/focus", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/login?next=/focus"
    assert secured.get("/api/focus").status_code == 401
    assert secured.post("/focus/add", data={"title": "X"}, follow_redirects=False).status_code == 303


def test_login_flow(secured):
    bad = secured.post("/login", data={"password": "nope", "next": "/focus"}, follow_redirects=False)
    assert "error=Wrong" in bad.headers["location"]

    ok = secured.post("/login", data={"password": "correct horse battery", "next": "//evil.com"},
                      follow_redirects=False)
    assert ok.headers["location"] == "/"  # no open redirect
    assert secured.get("/api/focus").status_code == 200

    secured.post("/logout")
    assert secured.get("/api/focus").status_code == 401


def test_cross_origin_post_refused(secured):
    resp = secured.post("/login", data={"password": "x"}, headers={"Origin": "https://evil.com"})
    assert resp.status_code == 403


def test_unconfigured_deploy_refuses(monkeypatch):
    monkeypatch.setenv("EDITOR_LOCAL", "0")
    monkeypatch.delenv("APP_PASSWORD_HASH", raising=False)
    with TestClient(app) as client:
        assert client.get("/").status_code == 503
