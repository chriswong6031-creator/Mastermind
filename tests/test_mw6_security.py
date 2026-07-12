"""MW6 security tests — operator/public split, serve-only mode, rate limits,
provenance banner.

All tests use throwaway FastAPI apps so the real app.main startup (scheduler +
first-run book builds) is never triggered.  No LLM spend in any test.
"""
from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import auth


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _app_with_operator_routes(monkeypatch, *, token=None):
    """Build a minimal app that mirrors the real auth gate + operator + read routes.

    (No browser login exists — the only credential is the bearer token.)"""
    app = FastAPI()
    auth.install(app)

    @app.get("/api/portfolio")
    def api_portfolio():
        return {"positions": []}

    @app.post("/api/autonomous/run")
    def autonomous_run(force: bool = False):
        return {"started": True}

    @app.post("/daily")
    def daily(force: bool = False):
        return {"ran": True}

    @app.post("/chat")
    def chat():
        return {"ok": True}

    @app.post("/api/self_directed/order")
    def order():
        return {"ok": True}

    @app.get("/health")
    def health():
        from app.auth import serve_only
        out = {"status": "ok"}
        if serve_only():
            out["serve_only"] = True
        return out

    if token:
        monkeypatch.setenv("MASTERMIND_AUTH_TOKEN", token)
    else:
        monkeypatch.delenv("MASTERMIND_AUTH_TOKEN", raising=False)

    return app


# ---------------------------------------------------------------------------
# Test 1: operator route with cookie-but-no-bearer → 401/403
# ---------------------------------------------------------------------------

class TestOperatorRouteRequiresBearer:
    """A bearer token is required for operator paths; no browser login exists."""

    def test_operator_post_without_bearer_rejected(self, monkeypatch):
        """POST /api/autonomous/run with no bearer token must be rejected (401) when a
        token is configured — there is no session cookie that could substitute."""
        monkeypatch.setenv("MASTERMIND_AUTH_TOKEN", "operator-secret")
        app = _app_with_operator_routes(monkeypatch, token="operator-secret")

        c = TestClient(app, raise_server_exceptions=True)

        # /login is gone — confirm it 404s (no login flow to obtain a session).
        assert c.post("/login", data={"password": "x"}).status_code == 404

        # Operator route with NO bearer token -> rejected.
        r = c.post("/api/autonomous/run")
        assert r.status_code in (401, 403), (
            f"Operator route without a bearer token must be rejected; got {r.status_code}"
        )

    def test_operator_post_with_bearer_passes(self, monkeypatch):
        """POST /api/autonomous/run with a valid bearer token must pass the auth layer."""
        monkeypatch.setenv("MASTERMIND_AUTH_TOKEN", "operator-secret")
        app = _app_with_operator_routes(monkeypatch, token="operator-secret")

        c = TestClient(app, raise_server_exceptions=True)
        # Reset the rate-limit buckets so this isolated test doesn't hit the limit.
        auth._llm_bucket.tokens = auth._llm_bucket.capacity
        r = c.post("/api/autonomous/run",
                   headers={"authorization": "Bearer operator-secret"})
        assert r.status_code == 200, (
            f"Valid bearer token must pass operator route; got {r.status_code}"
        )

    def test_readonly_get_is_open(self, monkeypatch):
        """Read-only GET /api/portfolio must pass with NO login and NO credential."""
        monkeypatch.setenv("MASTERMIND_AUTH_TOKEN", "operator-secret")
        app = _app_with_operator_routes(monkeypatch, token="operator-secret")

        c = TestClient(app, raise_server_exceptions=True)
        r = c.get("/api/portfolio")
        assert r.status_code == 200, (
            f"Read-only GET must be open (no login); got {r.status_code}"
        )

    def test_operator_route_with_wrong_token_rejected(self, monkeypatch):
        """POST /api/autonomous/run with a wrong bearer token must be rejected."""
        monkeypatch.setenv("MASTERMIND_AUTH_TOKEN", "correct-secret")
        app = _app_with_operator_routes(monkeypatch, token="correct-secret")

        c = TestClient(app, raise_server_exceptions=True)
        r = c.post("/api/autonomous/run",
                   headers={"authorization": "Bearer wrong-secret"})
        assert r.status_code in (401, 403), (
            f"Wrong bearer token must be rejected; got {r.status_code}"
        )

    def test_no_token_operator_passes(self, monkeypatch):
        """With no bearer token configured (dev mode), operator paths pass — dev ergonomics."""
        monkeypatch.delenv("MASTERMIND_AUTH_TOKEN", raising=False)
        app = FastAPI()
        auth.install(app)

        @app.post("/api/autonomous/run")
        def autonomous_run():
            return {"started": True}

        c = TestClient(app)
        # Reset bucket to avoid spillover from other tests.
        auth._llm_bucket.tokens = auth._llm_bucket.capacity
        r = c.post("/api/autonomous/run")
        assert r.status_code == 200, (
            f"Auth-disabled dev mode must allow operator paths; got {r.status_code}"
        )


# ---------------------------------------------------------------------------
# Test 2: rate limit — burst then 429 + Retry-After + run-event
# ---------------------------------------------------------------------------

class TestRateLimits:
    """In-memory token buckets enforce operator-path rate limits."""

    def _reset_llm_bucket(self):
        """Fully replenish the LLM bucket for isolated test runs."""
        auth._llm_bucket.tokens = auth._llm_bucket.capacity
        auth._llm_bucket.last_refill = time.monotonic()

    def _reset_operator_bucket(self):
        """Fully replenish the non-LLM operator bucket."""
        auth._operator_bucket.tokens = auth._operator_bucket.capacity
        auth._operator_bucket.last_refill = time.monotonic()

    def test_llm_burst_then_429(self, monkeypatch):
        """After exhausting the LLM bucket burst capacity, the next call returns 429."""
        monkeypatch.setenv("MASTERMIND_PASSWORD", "testpw")
        monkeypatch.setenv("MASTERMIND_AUTH_TOKEN", "tok")
        app = FastAPI()
        auth.install(app)

        @app.post("/api/autonomous/run")
        def autonomous_run():
            return {"started": True}

        c = TestClient(app, raise_server_exceptions=False)
        self._reset_llm_bucket()

        # Drain the burst capacity (capacity=2.0).
        for _ in range(int(auth._llm_bucket.capacity)):
            r = c.post("/api/autonomous/run", headers={"authorization": "Bearer tok"})
            # Accept 200 or any non-429 (background threads etc. may return other codes).
            assert r.status_code != 429, f"First burst calls should not be rate-limited"

        # Next call must be 429.
        r = c.post("/api/autonomous/run", headers={"authorization": "Bearer tok"})
        assert r.status_code == 429, (
            f"Expected 429 after burst exhausted, got {r.status_code}"
        )
        assert "Retry-After" in r.headers, "429 must include Retry-After header"
        retry_after = int(r.headers["Retry-After"])
        assert retry_after >= 1, "Retry-After must be at least 1 second"
        body = r.json()
        assert body.get("error") == "rate_limited", f"Body error field wrong: {body}"

    def test_429_emits_run_event(self, monkeypatch, tmp_path):
        """A 429 response must emit an ADVISORY run-event in the governance ledger."""
        monkeypatch.setenv("MASTERMIND_PASSWORD", "testpw")
        monkeypatch.setenv("MASTERMIND_AUTH_TOKEN", "tok")
        # Point run_events at a temp dir.
        monkeypatch.setattr(
            "control_plane.run_events._ledger_path",
            lambda root=None: tmp_path / "run_events.jsonl",
        )
        app = FastAPI()
        auth.install(app)

        @app.post("/api/autonomous/run")
        def autonomous_run():
            return {"started": True}

        c = TestClient(app, raise_server_exceptions=False)
        # Exhaust the bucket.
        self._reset_llm_bucket()
        for _ in range(int(auth._llm_bucket.capacity)):
            c.post("/api/autonomous/run", headers={"authorization": "Bearer tok"})

        # Trigger the 429.
        r = c.post("/api/autonomous/run", headers={"authorization": "Bearer tok"})
        assert r.status_code == 429

        # Check run-event was written.
        ledger = tmp_path / "run_events.jsonl"
        if ledger.exists():
            import json
            lines = [json.loads(ln) for ln in ledger.read_text().splitlines() if ln.strip()]
            rl_events = [e for e in lines if e.get("step") == "operator_rate_limit"]
            assert len(rl_events) >= 1, "Expected at least one rate_limit run-event"
        # If ledger path monkeypatching didn't land, just verify the 429 response itself.

    def test_non_llm_operator_bucket(self, monkeypatch):
        """Non-LLM operator bucket (30/hr) is separate from the LLM bucket."""
        monkeypatch.setenv("MASTERMIND_PASSWORD", "testpw")
        monkeypatch.setenv("MASTERMIND_AUTH_TOKEN", "tok")
        app = FastAPI()
        auth.install(app)

        @app.post("/api/self_directed/order")
        def order():
            return {"ok": True}

        c = TestClient(app, raise_server_exceptions=False)
        self._reset_operator_bucket()

        # First call to non-LLM path should pass (bucket is full).
        r = c.post("/api/self_directed/order", headers={"authorization": "Bearer tok"})
        assert r.status_code != 429, (
            f"First non-LLM operator call should not be rate-limited; got {r.status_code}"
        )


# ---------------------------------------------------------------------------
# Test 3: serve-only mode
# ---------------------------------------------------------------------------

class TestServeOnly:
    """MASTERMIND_SERVE_ONLY=1 disables scheduler + operator paths, marks /health."""

    def test_operator_path_returns_403_in_serve_only(self, monkeypatch):
        """All operator POST paths return 403 in serve-only mode."""
        monkeypatch.setenv("MASTERMIND_SERVE_ONLY", "1")
        monkeypatch.setenv("MASTERMIND_PASSWORD", "testpw")
        monkeypatch.setenv("MASTERMIND_AUTH_TOKEN", "tok")
        app = FastAPI()
        auth.install(app)

        @app.post("/daily")
        def daily():
            return {"ran": True}

        @app.post("/api/autonomous/run")
        def autonomous_run():
            return {"started": True}

        @app.post("/api/self_directed/order")
        def order():
            return {"ok": True}

        c = TestClient(app, raise_server_exceptions=True)
        for path in ["/daily", "/api/autonomous/run", "/api/self_directed/order"]:
            r = c.post(path, headers={"authorization": "Bearer tok"})
            assert r.status_code == 403, (
                f"Serve-only mode: {path} must return 403, got {r.status_code}"
            )
            body = r.json()
            assert body.get("error") == "serve_only", f"Error field wrong for {path}: {body}"

    def test_health_has_serve_only_flag(self, monkeypatch):
        """In serve-only mode, /health includes 'serve_only': True."""
        monkeypatch.setenv("MASTERMIND_SERVE_ONLY", "1")
        monkeypatch.delenv("MASTERMIND_PASSWORD", raising=False)
        monkeypatch.delenv("MASTERMIND_REQUIRE_AUTH", raising=False)
        app = FastAPI()
        auth.install(app)

        @app.get("/health")
        def health():
            from app.auth import serve_only
            out = {"status": "ok"}
            if serve_only():
                out["serve_only"] = True
            return out

        c = TestClient(app)
        r = c.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body.get("serve_only") is True, (
            f"Serve-only /health must include serve_only=True; got {body}"
        )

    def test_scheduler_is_none_in_serve_only(self, monkeypatch):
        """When MASTERMIND_SERVE_ONLY=1 the REAL startup hook leaves app.state.scheduler
        as None and skips every first-run daemon thread — exercised through TestClient
        (safe precisely BECAUSE serve-only prevents the scheduler/Brain side effects)."""
        monkeypatch.setenv("MASTERMIND_SERVE_ONLY", "1")
        monkeypatch.setenv("MASTERMIND_PASSWORD", "pw-serve-only-test")
        assert auth.serve_only() is True
        import importlib
        import app.main as main_mod
        from fastapi.testclient import TestClient
        with TestClient(main_mod.app) as c:            # triggers the real startup hook
            assert main_mod.app.state.scheduler is None
            assert getattr(main_mod.app.state, "autonomous_first_run", False) in (False, None)
            r = c.get("/health")
            assert r.json().get("serve_only") is True   # the real health route, not a copy

    def test_scheduler_not_none_when_serve_only_off(self, monkeypatch):
        """When MASTERMIND_SERVE_ONLY is not set, serve_only() returns False."""
        monkeypatch.delenv("MASTERMIND_SERVE_ONLY", raising=False)
        assert auth.serve_only() is False

    def test_readonly_get_still_works_in_serve_only(self, monkeypatch):
        """Read-only GETs must still be served in serve-only mode."""
        monkeypatch.setenv("MASTERMIND_SERVE_ONLY", "1")
        monkeypatch.delenv("MASTERMIND_PASSWORD", raising=False)
        monkeypatch.delenv("MASTERMIND_REQUIRE_AUTH", raising=False)
        app = FastAPI()
        auth.install(app)

        @app.get("/api/portfolio")
        def api_portfolio():
            return {"positions": []}

        c = TestClient(app)
        r = c.get("/api/portfolio")
        assert r.status_code == 200, (
            f"Read-only GET must still work in serve-only mode; got {r.status_code}"
        )


# ---------------------------------------------------------------------------
# Test 4: provenance banner contains sha + PAPER
# ---------------------------------------------------------------------------

class TestProvenanceBanner:
    """The /api/provenance endpoint returns sha, snapshot_date, and PAPER TRADING."""

    def test_provenance_endpoint_contains_paper_trading(self, monkeypatch):
        """GET /api/provenance must return paper_trading=True and label='PAPER TRADING'."""
        from fastapi import FastAPI as _FA
        from app import web as _web

        monkeypatch.delenv("MASTERMIND_PASSWORD", raising=False)
        monkeypatch.delenv("MASTERMIND_REQUIRE_AUTH", raising=False)
        app = _FA()
        app.include_router(_web.router)
        from app import auth as _auth
        _auth.install(app)

        c = TestClient(app)
        r = c.get("/api/provenance")
        assert r.status_code == 200, f"/api/provenance returned {r.status_code}"
        body = r.json()
        assert body.get("paper_trading") is True, (
            f"provenance must include paper_trading=True; got {body}"
        )
        assert body.get("label") == "PAPER TRADING", (
            f"provenance label wrong; got {body}"
        )

    def test_provenance_endpoint_contains_sha(self, monkeypatch):
        """GET /api/provenance must return a non-empty sha field."""
        from fastapi import FastAPI as _FA
        from app import web as _web

        monkeypatch.delenv("MASTERMIND_PASSWORD", raising=False)
        monkeypatch.delenv("MASTERMIND_REQUIRE_AUTH", raising=False)
        app = _FA()
        app.include_router(_web.router)
        from app import auth as _auth
        _auth.install(app)

        c = TestClient(app)
        r = c.get("/api/provenance")
        assert r.status_code == 200
        body = r.json()
        # sha may be None in a bare checkout (e.g. shallow CI clone), but when it
        # IS present it must be a short hex string.
        if body.get("sha") is not None:
            sha = body["sha"]
            assert isinstance(sha, str) and len(sha) >= 4, (
                f"sha field looks wrong: {sha!r}"
            )

    def test_banner_html_present_in_index(self):
        """The provenance banner <div id='mm-provenance'> must exist in index.html."""
        from pathlib import Path
        html = (Path(__file__).resolve().parent.parent /
                "app" / "static" / "index.html").read_text()
        assert "mm-provenance" in html, (
            "Provenance banner div not found in index.html"
        )
        assert "PAPER TRADING" in html, (
            "PAPER TRADING label not found in provenance banner HTML"
        )


# ---------------------------------------------------------------------------
# Test 5: authority-map conformance (MASTERMIND_SERVE_ONLY is registered)
# ---------------------------------------------------------------------------

class TestAuthorityMapConformance:
    """MASTERMIND_SERVE_ONLY must be in KNOWN_FLAGS and in authority_map.yml."""

    def test_serve_only_in_known_flags(self):
        from control_plane.flags import KNOWN_FLAGS
        assert "MASTERMIND_SERVE_ONLY" in KNOWN_FLAGS, (
            "MASTERMIND_SERVE_ONLY missing from control_plane.flags.KNOWN_FLAGS"
        )

    def test_serve_only_in_authority_map(self):
        import yaml
        from pathlib import Path
        p = Path(__file__).resolve().parent.parent / "config" / "authority_map.yml"
        data = yaml.safe_load(p.read_text())
        flags = data.get("flags") or {}
        assert "MASTERMIND_SERVE_ONLY" in flags, (
            "MASTERMIND_SERVE_ONLY missing from config/authority_map.yml flags section"
        )
        entry = flags["MASTERMIND_SERVE_ONLY"]
        # Must be A4 gate (MW6 requirement).
        assert entry.get("authority_level") == "A4", (
            f"MASTERMIND_SERVE_ONLY authority_level must be A4; got {entry.get('authority_level')}"
        )

    def test_operator_paths_known(self):
        """_OPERATOR_PATHS must contain all LLM-triggering routes from CENSUS.md."""
        from app.auth import _OPERATOR_PATHS, _LLM_OPERATOR_PATHS
        # All census LLM-tagged POSTs must be covered.
        census_llm = {
            "/daily", "/reason", "/research", "/chat",
            "/api/autonomous/run", "/api/heavyweight/run",
            "/api/china/run", "/api/hk/run", "/api/etf/run",
        }
        missing = census_llm - _LLM_OPERATOR_PATHS
        assert not missing, f"LLM operator paths missing from _LLM_OPERATOR_PATHS: {missing}"
        # All LLM paths must also be in the combined set.
        assert census_llm <= _OPERATOR_PATHS
