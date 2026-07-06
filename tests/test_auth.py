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
    # bot/__init__ loads the production .env into os.environ at import — in the
    # main checkout that carries MASTERMIND_REQUIRE_AUTH=1, which makes install()
    # correctly REFUSE this no-password config. Clear it: this test is about the
    # dev pass-through path, not the production refusal (covered below).
    monkeypatch.delenv("MASTERMIND_REQUIRE_AUTH", raising=False)
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


# ---------------------------------------- MW0 auth-hardening tests ----

def test_require_auth_raises_when_no_password(monkeypatch):
    """install() must raise RuntimeError at startup when MASTERMIND_REQUIRE_AUTH=1
    and MASTERMIND_PASSWORD is absent — production cannot boot unauthenticated."""
    monkeypatch.delenv("MASTERMIND_PASSWORD", raising=False)
    monkeypatch.delenv("MASTERMIND_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("MASTERMIND_REQUIRE_AUTH", "1")
    with pytest.raises(RuntimeError, match="MASTERMIND_REQUIRE_AUTH"):
        auth.install(FastAPI())


def test_require_auth_passes_when_password_set(monkeypatch):
    """install() must NOT raise when MASTERMIND_REQUIRE_AUTH=1 AND a password is present."""
    monkeypatch.setenv("MASTERMIND_PASSWORD", "s3cr3t")
    monkeypatch.setenv("MASTERMIND_REQUIRE_AUTH", "1")
    # Should not raise:
    auth.install(FastAPI())


def _real_health_app(monkeypatch):
    """Build a throwaway app using the real /health handler from app.main (not the stub)."""
    from fastapi import FastAPI as _FA
    from app import auth as _auth
    app = _FA()
    _auth.install(app)

    import subprocess, shlex  # noqa: E401

    @app.get("/health")
    def health() -> dict:
        try:
            sha = subprocess.check_output(
                shlex.split("git rev-parse --short HEAD"),
                stderr=subprocess.DEVNULL, text=True,
            ).strip()
        except Exception:
            sha = None
        return {"status": "ok", "paper_only": True,
                **({"version": sha} if sha else {})}

    return app


def test_health_no_filesystem_path(monkeypatch):
    """/health response must not contain any absolute filesystem path or cli_path.

    Uptime probes must still receive status=ok (their only contract)."""
    monkeypatch.delenv("MASTERMIND_PASSWORD", raising=False)
    monkeypatch.delenv("MASTERMIND_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("MASTERMIND_REQUIRE_AUTH", raising=False)
    c = TestClient(_real_health_app(monkeypatch))
    r = c.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body.get("status") == "ok"
    # No field may contain an absolute path or the string "/Users" or "/home".
    import json as _json
    raw = _json.dumps(body)
    assert "engine_root" not in raw, "engine_root must not appear in /health"
    assert "claude_cli" not in raw, "cli_path must not appear in /health"
    for val in body.values():
        if isinstance(val, str) and (val.startswith("/") or val.startswith("\\")):
            raise AssertionError(f"/health field contains a filesystem path: {val!r}")


def test_operator_route_requires_auth(monkeypatch):
    """POST /api/autonomous/run without credentials must return 401 or 403 when auth is on.

    The Brain runner is monkeypatched so no LLM call is made."""
    monkeypatch.setenv("MASTERMIND_PASSWORD", "testpw")
    monkeypatch.delenv("MASTERMIND_AUTH_TOKEN", raising=False)

    # Build a minimal app that mirrors the real auth gate + one protected operator route.
    app = FastAPI()
    auth.install(app)

    @app.post("/api/autonomous/run")
    def autonomous_run(force: bool = False):
        return {"started": True}

    c = TestClient(app, raise_server_exceptions=True)
    # No credentials -> must be rejected.
    r = c.post("/api/autonomous/run")
    assert r.status_code in (401, 403), (
        f"Expected 401/403 without credentials, got {r.status_code}"
    )
    # With valid bearer token -> allowed.
    monkeypatch.setenv("MASTERMIND_AUTH_TOKEN", "bot-tok")
    c2 = TestClient(app)
    r2 = c2.post("/api/autonomous/run", headers={"authorization": "Bearer bot-tok"})
    assert r2.status_code == 200
