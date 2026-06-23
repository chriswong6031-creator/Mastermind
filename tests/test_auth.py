"""Auth gate tests — built on a THROWAWAY FastAPI app so we never trigger the real
app.main startup (scheduler + first-run book builds)."""
from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import auth


# ------------------------------------------------------- signed cookie ----

def test_cookie_roundtrip_and_tamper():
    assert auth.verify_cookie(auth.make_cookie("hunter2"), "hunter2")
    # wrong password (derived key differs) -> reject
    assert not auth.verify_cookie(auth.make_cookie("hunter2"), "other")
    # tampered signature -> reject
    tok = auth.make_cookie("hunter2")
    assert not auth.verify_cookie(tok[:-1] + ("0" if tok[-1] != "0" else "1"), "hunter2")
    assert not auth.verify_cookie("garbage", "hunter2")
    assert not auth.verify_cookie(None, "hunter2")


def test_cookie_expiry():
    past = auth.make_cookie("pw", ttl_days=1, now=time.time() - 2 * 86400)
    assert not auth.verify_cookie(past, "pw")
    fresh = auth.make_cookie("pw", ttl_days=1)
    assert auth.verify_cookie(fresh, "pw")


def test_safe_next_blocks_open_redirect():
    assert auth.safe_next("/api/portfolio") == "/api/portfolio"
    assert auth.safe_next("//evil.com") == "/"
    assert auth.safe_next("https://evil.com") == "/"
    assert auth.safe_next(None) == "/"


# ----------------------------------------------------------- middleware ----

def _app() -> FastAPI:
    app = FastAPI()
    auth.install(app)

    @app.get("/secret")
    def secret():
        return {"ok": True}

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return app


def test_disabled_when_no_password(monkeypatch):
    monkeypatch.delenv("MASTERMIND_PASSWORD", raising=False)
    monkeypatch.delenv("MASTERMIND_AUTH_TOKEN", raising=False)
    c = TestClient(_app())
    assert c.get("/secret").status_code == 200      # pass-through


def test_api_blocked_then_login_flow(monkeypatch):
    monkeypatch.setenv("MASTERMIND_PASSWORD", "letmein")
    monkeypatch.delenv("MASTERMIND_AUTH_TOKEN", raising=False)
    c = TestClient(_app())

    # XHR/API with no session -> 401 JSON
    r = c.get("/secret")
    assert r.status_code == 401 and r.json()["error"] == "unauthorized"

    # browser navigation -> 303 redirect to /login
    r = c.get("/secret", headers={"accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login?next=/secret")

    # /health stays open
    assert c.get("/health").status_code == 200

    # wrong password -> 401, no cookie
    r = c.post("/login", data={"password": "nope", "next": "/secret"}, follow_redirects=False)
    assert r.status_code == 401 and "mm_session" not in r.cookies

    # correct password -> 303 + Set-Cookie, then the gate opens
    r = c.post("/login", data={"password": "letmein", "next": "/secret"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/secret"
    assert "mm_session" in r.cookies
    assert c.get("/secret").status_code == 200       # cookie now carried by the client

    # logout clears it
    c.get("/logout", follow_redirects=False)
    c.cookies.clear()
    assert c.get("/secret").status_code == 401


def test_bearer_token(monkeypatch):
    monkeypatch.setenv("MASTERMIND_PASSWORD", "letmein")
    monkeypatch.setenv("MASTERMIND_AUTH_TOKEN", "s3cr3t-bot-token")
    c = TestClient(_app())
    assert c.get("/secret").status_code == 401
    r = c.get("/secret", headers={"authorization": "Bearer s3cr3t-bot-token"})
    assert r.status_code == 200
    r = c.get("/secret", headers={"authorization": "Bearer wrong"})
    assert r.status_code == 401
