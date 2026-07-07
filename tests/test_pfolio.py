"""Tests for app.pfolio — Portfolio Risk Desk CRUD proxy (W1).

Covers:
- ticker normalization
- totals math (shares-null handling)
- PostgREST URL/filter construction (operator scoping on every verb)
- fail-soft on missing env
- serve-only carve-out (POST /api/pfolio/* passes when MASTERMIND_SERVE_ONLY=1,
  while a standard operator POST stays blocked)
- missing-table error shape

No network calls: httpx and yahoo_feed are fully mocked.
"""
from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch, PropertyMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_app(monkeypatch, *, supabase_url="http://sb.test", service_key="svc-key",
              operator_email="demo@mastermind.test", uid="uid-1234"):
    """Build a minimal FastAPI app with pfolio + auth routers wired."""
    monkeypatch.setenv("SUPABASE_URL", supabase_url)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", service_key)
    monkeypatch.setenv("MASTERMIND_OPERATOR_EMAIL", operator_email)
    monkeypatch.delenv("MASTERMIND_PASSWORD", raising=False)
    monkeypatch.delenv("MASTERMIND_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("MASTERMIND_REQUIRE_AUTH", raising=False)
    monkeypatch.delenv("MASTERMIND_SERVE_ONLY", raising=False)

    from app import auth, pfolio

    # Reset module-level UID cache so env changes take effect
    pfolio._operator_uid_cache.clear()

    app = FastAPI()
    auth.install(app)
    app.include_router(pfolio.router)
    return app, uid


def _mock_uid_resolve(monkeypatch, uid):
    """Patch operator UID resolution to return a fixed UID without network."""
    monkeypatch.setattr(
        "app.pfolio._resolve_operator_uid",
        lambda: uid,
    )


def _httpx_resp(status: int, body) -> MagicMock:
    """Build a mock httpx response."""
    resp = MagicMock()
    resp.status_code = status
    resp.content = b"1"  # truthy
    resp.json.return_value = body
    return resp


# ---------------------------------------------------------------------------
# 1. Ticker normalization
# ---------------------------------------------------------------------------

def test_normalize_ticker_basic():
    from app.pfolio import _normalize_ticker
    assert _normalize_ticker("aapl") == "AAPL"
    assert _normalize_ticker("  nvda  ") == "NVDA"
    assert _normalize_ticker("0700.HK") == "0700.HK"
    assert _normalize_ticker("600519.SS") == "600519.SS"
    assert _normalize_ticker("BRK-B") == "BRK-B"
    assert _normalize_ticker("BRK.B") == "BRK.B"


def test_normalize_ticker_invalid():
    from app.pfolio import _normalize_ticker
    assert _normalize_ticker("") is None
    assert _normalize_ticker("   ") is None
    # Too long (>20 chars)
    assert _normalize_ticker("A" * 21) is None
    # Contains disallowed chars
    assert _normalize_ticker("AAPL!") is None
    assert _normalize_ticker("A B") is None


def test_normalize_ticker_case_upper():
    from app.pfolio import _normalize_ticker
    assert _normalize_ticker("smh") == "SMH"
    assert _normalize_ticker("spy") == "SPY"


# ---------------------------------------------------------------------------
# 2. Totals math — shares-null handling
# ---------------------------------------------------------------------------

def test_portfolio_totals_all_null_shares():
    from app.pfolio import _portfolio_totals
    positions = [
        {"ticker": "AAPL", "shares": None, "market_value": None, "day_change_pct": None,
         "last": 150.0, "pnl_usd": None},
        {"ticker": "MSFT", "shares": None, "market_value": None, "day_change_pct": 1.5,
         "last": 300.0, "pnl_usd": None},
    ]
    result = _portfolio_totals(positions)
    assert result["total_value"] is None
    assert result["day_pnl"] is None
    assert result["total_pnl_usd"] is None


def test_portfolio_totals_mixed_shares():
    from app.pfolio import _portfolio_totals
    positions = [
        {"ticker": "AAPL", "shares": 100.0, "market_value": 15000.0,
         "day_change_pct": 2.0, "last": 150.0, "pnl_usd": 500.0},
        {"ticker": "MSFT", "shares": None, "market_value": None,
         "day_change_pct": 1.5, "last": 300.0, "pnl_usd": None},
    ]
    result = _portfolio_totals(positions)
    assert result["total_value"] == 15000.0
    # only AAPL has shares: day_pnl = shares*(last - last/1.02)
    assert result["day_pnl"] is not None and result["day_pnl"] != 0
    assert result["total_pnl_usd"] == 500.0


def test_portfolio_totals_zero_pnl():
    from app.pfolio import _portfolio_totals
    positions = [
        {"ticker": "SPY", "shares": 50.0, "market_value": 22500.0,
         "day_change_pct": 0.0, "last": 450.0, "pnl_usd": 0.0},
    ]
    result = _portfolio_totals(positions)
    assert result["total_value"] == 22500.0
    assert result["total_pnl_usd"] == 0.0


def test_portfolio_totals_multiple_positions():
    from app.pfolio import _portfolio_totals
    positions = [
        {"ticker": "A", "shares": 10.0, "market_value": 1000.0,
         "day_change_pct": 1.0, "last": 100.0, "pnl_usd": 100.0},
        {"ticker": "B", "shares": 20.0, "market_value": 4000.0,
         "day_change_pct": -2.0, "last": 200.0, "pnl_usd": -200.0},
    ]
    result = _portfolio_totals(positions)
    assert result["total_value"] == 5000.0
    assert result["total_pnl_usd"] == -100.0


# ---------------------------------------------------------------------------
# 3. Fail-soft on missing env
# ---------------------------------------------------------------------------

def test_fail_soft_no_supabase_env(monkeypatch):
    """When SUPABASE_URL/SERVICE_ROLE_KEY are absent, all endpoints return
    {"ok": false, "error": "supabase_unavailable"} with HTTP 200."""
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.delenv("MASTERMIND_PASSWORD", raising=False)
    monkeypatch.delenv("MASTERMIND_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("MASTERMIND_REQUIRE_AUTH", raising=False)
    monkeypatch.delenv("MASTERMIND_SERVE_ONLY", raising=False)

    from app import auth, pfolio
    pfolio._operator_uid_cache.clear()
    app = FastAPI()
    auth.install(app)
    app.include_router(pfolio.router)
    client = TestClient(app)

    r = client.get("/api/pfolio/positions")
    assert r.status_code == 200
    assert r.json()["error"] == "supabase_unavailable"

    r = client.post("/api/pfolio/positions", json={"ticker": "AAPL"})
    assert r.status_code == 200
    assert r.json()["error"] == "supabase_unavailable"


# ---------------------------------------------------------------------------
# 4. PostgREST URL/filter construction — operator scoping
# ---------------------------------------------------------------------------

def test_get_positions_operator_scoped(monkeypatch):
    """GET /api/pfolio/positions must include user_id=eq.{uid} in the PostgREST query."""
    uid = "test-uid-abc"
    app, _ = _make_app(monkeypatch, uid=uid)
    _mock_uid_resolve(monkeypatch, uid)

    captured = {}

    def mock_get(url, params=None, headers=None, timeout=None):
        captured["url"] = url
        captured["params"] = params or {}
        return _httpx_resp(200, [])

    with patch("httpx.get", mock_get):
        client = TestClient(app)
        r = client.get("/api/pfolio/positions")

    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert f"eq.{uid}" in str(captured["params"].get("user_id", ""))
    assert "portfolio_positions" in captured["url"]


def test_create_position_operator_scoped(monkeypatch):
    """POST /api/pfolio/positions must include user_id = operator uid in the row body."""
    uid = "op-uid-xyz"
    app, _ = _make_app(monkeypatch, uid=uid)
    _mock_uid_resolve(monkeypatch, uid)

    captured = {}

    def mock_post(url, json=None, headers=None, timeout=None):
        captured["body"] = json or {}
        captured["url"] = url
        row = {**(json or {}), "id": "new-id-1", "status": "open"}
        return _httpx_resp(201, [row])

    with patch("httpx.post", mock_post):
        client = TestClient(app)
        r = client.post("/api/pfolio/positions", json={"ticker": "NVDA", "shares": 50.0})

    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert captured["body"].get("user_id") == uid
    assert captured["body"].get("ticker") == "NVDA"


def test_patch_position_operator_scoped(monkeypatch):
    """PATCH /api/pfolio/positions/{id} must pass user_id=eq.{uid} as a filter."""
    uid = "op-uid-patch"
    app, _ = _make_app(monkeypatch, uid=uid)
    _mock_uid_resolve(monkeypatch, uid)

    captured = {}

    def mock_patch(url, json=None, params=None, headers=None, timeout=None):
        captured["params"] = params or {}
        captured["url"] = url
        return _httpx_resp(200, [{"id": "pos-1", "shares": 75.0}])

    with patch("httpx.patch", mock_patch):
        client = TestClient(app)
        r = client.patch("/api/pfolio/positions/pos-1", json={"shares": 75.0})

    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert f"eq.{uid}" in str(captured["params"].get("user_id", ""))
    assert "eq.pos-1" in str(captured["params"].get("id", ""))


def test_delete_position_operator_scoped(monkeypatch):
    """DELETE /api/pfolio/positions/{id} must pass user_id=eq.{uid} as a filter."""
    uid = "op-uid-del"
    app, _ = _make_app(monkeypatch, uid=uid)
    _mock_uid_resolve(monkeypatch, uid)

    captured = {}

    def mock_delete(url, params=None, headers=None, timeout=None):
        captured["params"] = params or {}
        return _httpx_resp(204, None)

    with patch("httpx.delete", mock_delete):
        client = TestClient(app)
        r = client.delete("/api/pfolio/positions/pos-del-1")

    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert f"eq.{uid}" in str(captured["params"].get("user_id", ""))
    assert "eq.pos-del-1" in str(captured["params"].get("id", ""))


# ---------------------------------------------------------------------------
# 5. Missing-table error shape
# ---------------------------------------------------------------------------

def test_missing_table_error_shape(monkeypatch):
    """When PostgREST returns 404 with code 42P01, the endpoint returns
    {"ok": false, "error": "table_missing", "setup": "run sql/0002_portfolio_positions.sql"}."""
    uid = "op-uid-miss"
    app, _ = _make_app(monkeypatch, uid=uid)
    _mock_uid_resolve(monkeypatch, uid)

    def mock_get(url, params=None, headers=None, timeout=None):
        return _httpx_resp(404, {"code": "42P01", "message": "relation does not exist"})

    with patch("httpx.get", mock_get):
        client = TestClient(app)
        r = client.get("/api/pfolio/positions")

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == "table_missing"
    assert "0002_portfolio_positions.sql" in body["setup"]


def test_missing_table_on_post(monkeypatch):
    """POST also returns table_missing shape when PostgREST 404."""
    uid = "op-uid-miss2"
    app, _ = _make_app(monkeypatch, uid=uid)
    _mock_uid_resolve(monkeypatch, uid)

    def mock_post(url, json=None, headers=None, timeout=None):
        return _httpx_resp(404, {"code": "42P01", "message": "relation does not exist"})

    with patch("httpx.post", mock_post):
        client = TestClient(app)
        r = client.post("/api/pfolio/positions", json={"ticker": "AAPL"})

    assert r.status_code == 200
    body = r.json()
    assert body["error"] == "table_missing"


# ---------------------------------------------------------------------------
# 6. Serve-only carve-out (PRD-R8)
# ---------------------------------------------------------------------------

def test_serve_only_allows_pfolio_post(monkeypatch):
    """POST /api/pfolio/* must NOT be blocked by MASTERMIND_SERVE_ONLY=1."""
    uid = "op-uid-so"
    monkeypatch.setenv("MASTERMIND_SERVE_ONLY", "1")
    app, _ = _make_app(monkeypatch, uid=uid)
    # Re-apply SERVE_ONLY after _make_app which deletes it
    monkeypatch.setenv("MASTERMIND_SERVE_ONLY", "1")
    _mock_uid_resolve(monkeypatch, uid)

    def mock_post(url, json=None, headers=None, timeout=None):
        row = {**(json or {}), "id": "so-id-1", "status": "open"}
        return _httpx_resp(201, [row])

    with patch("httpx.post", mock_post):
        client = TestClient(app)
        r = client.post("/api/pfolio/positions", json={"ticker": "AAPL"})

    # Must NOT be 403 serve_only block
    assert r.status_code == 200
    body = r.json()
    assert body.get("error") != "serve_only"
    # Should succeed (ok=True) or fail-soft (supabase_unavailable) but NOT serve_only
    assert body.get("error") in (None, "supabase_unavailable")


def test_serve_only_blocks_standard_operator_post(monkeypatch):
    """Standard operator POST paths (e.g. /daily) must still be blocked in serve-only mode."""
    monkeypatch.setenv("MASTERMIND_SERVE_ONLY", "1")
    monkeypatch.setenv("MASTERMIND_PASSWORD", "pw")
    monkeypatch.setenv("MASTERMIND_AUTH_TOKEN", "tok")
    monkeypatch.delenv("MASTERMIND_REQUIRE_AUTH", raising=False)

    from app import auth
    auth.reset_rate_buckets()

    mini_app = FastAPI()
    auth.install(mini_app)

    @mini_app.post("/daily")
    def _daily():
        return {"ran": True}

    client = TestClient(mini_app)
    r = client.post("/daily", headers={"Authorization": "Bearer tok"})
    assert r.status_code == 403
    assert r.json()["error"] == "serve_only"


def test_pfolio_still_requires_auth(monkeypatch):
    """PRD-R8 carve-out exempts pfolio from serve-only blocking, but NOT from auth.
    When auth is enabled, unauthenticated requests to /api/pfolio/* get 401."""
    monkeypatch.setenv("MASTERMIND_PASSWORD", "secret")
    monkeypatch.delenv("MASTERMIND_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("MASTERMIND_REQUIRE_AUTH", raising=False)
    monkeypatch.delenv("MASTERMIND_SERVE_ONLY", raising=False)

    from app import auth, pfolio
    pfolio._operator_uid_cache.clear()
    app = FastAPI()
    auth.install(app)
    app.include_router(pfolio.router)
    client = TestClient(app)

    # No cookie, no bearer -> 401
    r = client.get("/api/pfolio/positions")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# 7. Ticker validation on create
# ---------------------------------------------------------------------------

def test_create_invalid_ticker_returns_422(monkeypatch):
    uid = "op-uid-val"
    app, _ = _make_app(monkeypatch, uid=uid)
    _mock_uid_resolve(monkeypatch, uid)

    client = TestClient(app)
    r = client.post("/api/pfolio/positions", json={"ticker": ""})
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_ticker"


def test_create_negative_shares_returns_422(monkeypatch):
    uid = "op-uid-neg"
    app, _ = _make_app(monkeypatch, uid=uid)
    _mock_uid_resolve(monkeypatch, uid)

    client = TestClient(app)
    r = client.post("/api/pfolio/positions", json={"ticker": "AAPL", "shares": -10.0})
    assert r.status_code == 422
    assert r.json()["error"] == "shares_must_be_positive"


def test_create_zero_entry_price_returns_422(monkeypatch):
    uid = "op-uid-zero"
    app, _ = _make_app(monkeypatch, uid=uid)
    _mock_uid_resolve(monkeypatch, uid)

    client = TestClient(app)
    r = client.post("/api/pfolio/positions", json={"ticker": "AAPL", "entry_price": 0.0})
    assert r.status_code == 422
    assert r.json()["error"] == "entry_price_must_be_positive"


# ---------------------------------------------------------------------------
# 8. No network in yahoo_feed during enrichment (mock it out)
# ---------------------------------------------------------------------------

def test_enrich_quotes_graceful_on_import_error():
    """_enrich_quotes must degrade gracefully when yfinance is unavailable."""
    from app.pfolio import _enrich_quotes

    positions = [
        {"id": "p1", "ticker": "AAPL", "shares": 100.0, "entry_price": 150.0},
    ]
    with patch("data_layer.yahoo_feed.warm", side_effect=ImportError("no yfinance")):
        enriched = _enrich_quotes(positions)

    assert len(enriched) == 1
    # Should not raise; fields should be null
    assert enriched[0]["last"] is None
    assert enriched[0]["market_value"] is None
    assert enriched[0]["pnl_usd"] is None


def test_enrich_quotes_computes_market_value():
    """_enrich_quotes computes market_value = shares * last when quote available.

    Mocks only the two yahoo_feed callables — avoids touching yfinance directly
    since a prior test may have poisoned its sys.modules entry.
    """
    import sys
    from app.pfolio import _enrich_quotes

    positions = [
        {"id": "p1", "ticker": "SPY", "shares": 10.0, "entry_price": 400.0},
    ]

    def mock_warm(tickers):
        pass

    # Stub prev-close fetch at the yfinance level by temporarily replacing the
    # entire try/except block: patch _enrich_quotes's local yfinance import path
    # by removing yfinance from sys.modules so the inner try falls through.
    yf_backup = sys.modules.pop("yfinance", None)
    try:
        with patch("data_layer.yahoo_feed.warm", mock_warm), \
             patch("data_layer.yahoo_feed.price_local", return_value=450.0):
            enriched = _enrich_quotes(positions)
    finally:
        if yf_backup is not None:
            sys.modules["yfinance"] = yf_backup
        elif "yfinance" in sys.modules:
            del sys.modules["yfinance"]

    assert enriched[0]["last"] == 450.0
    assert enriched[0]["market_value"] == 4500.0
    assert enriched[0]["pnl_usd"] == pytest.approx(500.0)
    assert enriched[0]["pnl_pct"] == pytest.approx(12.5)


# ---------------------------------------------------------------------------
# 9. risk_state.json absent -> risk: null (W2 not yet run)
# ---------------------------------------------------------------------------

def test_risk_for_position_absent_file():
    """When data/portfolio_watch/risk_state.json does not exist, risk is None."""
    from app.pfolio import _risk_for_position
    with patch("pathlib.Path.exists", return_value=False):
        result = _risk_for_position("some-id", "AAPL")
    assert result is None
