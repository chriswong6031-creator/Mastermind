"""Auth-middleware tests — built on a THROWAWAY FastAPI app so we never trigger the
real app.main startup (scheduler + first-run book builds).

The browser password-cookie login flow was REMOVED (page-only scope). What remains
and is tested here: read access is always open (no login), and the bearer-token
OPERATOR tier gates mutating/LLM POSTs. Serve-only + rate limits live in
tests/test_mw6_security.py.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import auth


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


def test_read_access_is_open_no_login(monkeypatch):
    """No browser login exists: every GET is served without any credential,
    on both localhost and the VPS mirror."""
    monkeypatch.delenv("MASTERMIND_AUTH_TOKEN", raising=False)
    c = TestClient(_app())
    # Plain XHR/API GET -> 200 (was 401 under the old cookie gate).
    assert c.get("/secret").status_code == 200
    # Browser navigation GET -> 200, NOT a 303 redirect to a (now-missing) /login.
    r = c.get("/secret", headers={"accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 200
    # /health stays open.
    assert c.get("/health").status_code == 200


def test_no_login_or_logout_routes(monkeypatch):
    """The /login and /logout routes are gone — they must 404 now."""
    monkeypatch.delenv("MASTERMIND_AUTH_TOKEN", raising=False)
    c = TestClient(_app())
    assert c.get("/login").status_code == 404
    assert c.post("/login", data={"password": "x"}).status_code == 404
    assert c.get("/logout").status_code == 404


def test_read_access_open_even_with_bearer_configured(monkeypatch):
    """Configuring a bearer token gates only operator POSTs — reads stay open."""
    monkeypatch.setenv("MASTERMIND_AUTH_TOKEN", "s3cr3t-bot-token")
    c = TestClient(_app())
    assert c.get("/secret").status_code == 200            # no credential needed to read
    # A bearer header on a read is accepted (ignored) — still 200.
    r = c.get("/secret", headers={"authorization": "Bearer s3cr3t-bot-token"})
    assert r.status_code == 200


# ---------------------------------------- health-route hardening ----

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
    monkeypatch.delenv("MASTERMIND_AUTH_TOKEN", raising=False)
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


def test_operator_route_requires_bearer(monkeypatch):
    """POST /api/autonomous/run without a bearer token must return 401 when a token
    is configured; with the valid token it passes.

    The Brain runner is a stub so no LLM call is made."""
    monkeypatch.setenv("MASTERMIND_AUTH_TOKEN", "bot-tok")

    app = FastAPI()
    auth.install(app)

    @app.post("/api/autonomous/run")
    def autonomous_run(force: bool = False):
        return {"started": True}

    auth.reset_rate_buckets()
    c = TestClient(app, raise_server_exceptions=True)
    # No bearer -> rejected (401).
    r = c.post("/api/autonomous/run")
    assert r.status_code == 401, (
        f"Expected 401 without a bearer token, got {r.status_code}"
    )
    # With valid bearer token -> allowed.
    auth.reset_rate_buckets()
    r2 = c.post("/api/autonomous/run", headers={"authorization": "Bearer bot-tok"})
    assert r2.status_code == 200


def test_operator_route_passes_without_token_in_dev(monkeypatch):
    """With no bearer token configured (dev), operator paths pass — dev ergonomics."""
    monkeypatch.delenv("MASTERMIND_AUTH_TOKEN", raising=False)

    app = FastAPI()
    auth.install(app)

    @app.post("/api/autonomous/run")
    def autonomous_run(force: bool = False):
        return {"started": True}

    auth.reset_rate_buckets()
    c = TestClient(app)
    r = c.post("/api/autonomous/run")
    assert r.status_code == 200
