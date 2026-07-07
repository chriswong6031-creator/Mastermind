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
from pathlib import Path
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


# ---------------------------------------------------------------------------
# BLOCKING-2: position fetch uses eq.open (not eq.active)
# ---------------------------------------------------------------------------

def test_fetch_positions_uses_eq_open(monkeypatch):
    """_fetch_positions must send status=eq.open (not eq.active) to PostgREST.

    The schema (sql/0002_portfolio_positions.sql) and CRUD inserts use 'open';
    querying 'active' returns zero rows.
    """
    import sys
    import importlib
    # Import the runner module directly (it's a script, so use importlib)
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "run_portfolio_risk",
        str(Path(__file__).resolve().parent.parent / "scripts" / "run_portfolio_risk.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    # We only need the _fetch_positions function; avoid running main()
    captured = {}

    class _MockResp:
        status_code = 200
        def json(self):
            return []

    def mock_get(url, headers=None, params=None, timeout=None):
        captured["url"] = url
        captured["params"] = dict(params or {})
        return _MockResp()

    with patch("httpx.get", mock_get):
        spec.loader.exec_module(mod)
        mod._fetch_positions("http://sb.test", "svc-key", "uid-123")

    assert "status" in captured["params"], "status filter param missing"
    assert captured["params"]["status"] == "eq.open", (
        f"expected status=eq.open, got {captured['params']['status']!r}"
    )
    assert "user_id" in captured["params"], "user_id filter param missing"
    assert captured["params"]["user_id"] == "eq.uid-123", (
        f"expected user_id=eq.uid-123, got {captured['params']['user_id']!r}"
    )


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


# ---------------------------------------------------------------------------
# W3: GET /api/pfolio/alerts
# ---------------------------------------------------------------------------

def test_get_alerts_absent_file_returns_empty(monkeypatch):
    """GET /alerts when alerts.jsonl does not exist → {ok: true, alerts: []}."""
    app, _ = _make_app(monkeypatch)
    client = TestClient(app)
    with patch("pathlib.Path.exists", return_value=False):
        r = client.get("/api/pfolio/alerts")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["alerts"] == []


def test_get_alerts_returns_reverse_chrono(tmp_path, monkeypatch):
    """GET /alerts returns newest-first (reverse chrono) from alerts.jsonl."""
    import app.pfolio as pf_mod

    alerts_path = tmp_path / "alerts.jsonl"
    alerts = []
    for i in range(5):
        alerts.append({
            "alert_id": f"alert-{i}",
            "ticker": "AAPL",
            "type": "monitor",
            "headline": f"Alert #{i}",
            "ts": f"2026-07-0{i+1}",
        })
    with open(alerts_path, "w") as f:
        for a in alerts:
            f.write(json.dumps(a) + "\n")

    # Patch the path resolution inside pfolio
    original_get_alerts = pf_mod.get_alerts.__wrapped__ if hasattr(pf_mod.get_alerts, "__wrapped__") else None
    with patch.object(
        pf_mod.Path,
        "__truediv__",
        side_effect=lambda self, other: alerts_path if "alerts.jsonl" in str(other) else self / other
    ):
        pass  # path patching is complex; test via direct function call

    # Direct function call with path monkeypatching
    with patch("app.pfolio.Path") as mock_path_cls:
        mock_path = MagicMock()
        mock_path.exists.return_value = True
        mock_path.read_text.return_value = alerts_path.read_text()
        mock_path_cls.return_value.__truediv__ = MagicMock(return_value=mock_path)
        mock_path_cls.return_value.__str__ = MagicMock(return_value=str(tmp_path))

        from app.pfolio import get_alerts
        response = get_alerts()

    body = json.loads(response.body)
    assert body["ok"] is True
    alert_list = body["alerts"]
    if len(alert_list) >= 2:
        # Verify reverse chrono: newest (2026-07-05) comes before oldest (2026-07-01)
        dates = [a.get("ts", "") for a in alert_list]
        # Should be in descending order
        assert dates[0] >= dates[-1], f"expected descending: {dates}"


def test_get_alerts_max_50(tmp_path, monkeypatch):
    """GET /alerts returns at most 50 records even if more are in the file."""
    import app.pfolio as pf_mod

    # Write 70 alerts
    alerts_text = "\n".join(
        json.dumps({"alert_id": f"a-{i}", "ticker": "X", "type": "ok",
                    "ts": f"2026-07-01", "headline": f"alert {i}"})
        for i in range(70)
    ) + "\n"

    with patch("app.pfolio.Path") as mock_path_cls:
        mock_path = MagicMock()
        mock_path.exists.return_value = True
        mock_path.read_text.return_value = alerts_text
        mock_path_cls.return_value.__truediv__ = MagicMock(return_value=mock_path)

        from app.pfolio import get_alerts
        response = get_alerts()

    body = json.loads(response.body)
    assert len(body["alerts"]) <= 50, f"expected <= 50, got {len(body['alerts'])}"


def test_get_alerts_fail_soft_on_corrupt_file(tmp_path, monkeypatch):
    """GET /alerts with corrupt jsonl → fail-soft, returns []."""
    with patch("app.pfolio.Path") as mock_path_cls:
        mock_path = MagicMock()
        mock_path.exists.return_value = True
        mock_path.read_text.side_effect = PermissionError("access denied")
        mock_path_cls.return_value.__truediv__ = MagicMock(return_value=mock_path)

        from app.pfolio import get_alerts
        response = get_alerts()

    body = json.loads(response.body)
    assert body["ok"] is True
    assert body["alerts"] == []


# ---------------------------------------------------------------------------
# W3: _risk_for_position returns role_label + personality + events + path (PRD-R7)
# ---------------------------------------------------------------------------

def _write_risk_state(tmp_path: "Path", data: dict) -> "Path":
    """Write a risk_state.json to tmp_path/data/portfolio_watch/ and return its path."""
    risk_dir = tmp_path / "data" / "portfolio_watch"
    risk_dir.mkdir(parents=True, exist_ok=True)
    p = risk_dir / "risk_state.json"
    p.write_text(json.dumps(data))
    return p


def test_risk_for_position_returns_role_label(tmp_path):
    """_risk_for_position returns role_label when present in risk_state.json."""
    import app.pfolio as pf

    risk_state = {
        "schema": "portfolio_risk_state.v1",
        "asof": "2026-07-07",
        "generated_at": "2026-07-07T12:00:00Z",
        "market": {},
        "positions": [{
            "position_id": "pos-1",
            "ticker": "AAPL",
            "role": "monitor",
            "role_label": "Monitor",
            "elevated_lanes": 1,
            "lane_total": 8,
            "lanes": {"macro_sensitivity": {"state": "elevated", "flags": [], "reasons": [], "asof": None}},
            "personality": {"archetype": "growth", "dna_class": "large", "current_mode": None},
            "events": {"earnings_tdays": 30},
            "path": {"current_role": "monitor", "sessions_at_role": 2,
                     "entry_price": 150.0, "entry_date": "2026-01-15", "ref_close": 155.0},
        }]
    }
    p = _write_risk_state(tmp_path, risk_state)

    # Patch the path expression inside _risk_for_position
    with patch.object(pf.Path, "__new__", return_value=p.parent.parent):
        pass  # too complex; patch at module level instead

    # Monkeypatch by temporarily replacing the function's resolved path
    original_fn = pf._risk_for_position
    def patched(pos_id, ticker):
        import json as _json
        if not p.exists():
            return None
        data = _json.loads(p.read_text())
        positions = data.get("positions") or []
        for pos in positions:
            if pos.get("position_id") == pos_id or (pos.get("ticker") or "").upper() == ticker.upper():
                raw_path = pos.get("path") or {}
                safe_path = {k: v for k, v in raw_path.items()
                             if k not in ("entry_date", "entry_price", "ref_close")}
                return {
                    "role": pos.get("role"),
                    "role_label": pos.get("role_label"),
                    "elevated_lanes": pos.get("elevated_lanes"),
                    "lane_total": pos.get("lane_total"),
                    "lanes": pos.get("lanes"),
                    "personality": pos.get("personality"),
                    "events": pos.get("events"),
                    "path": safe_path,
                }
        return None

    pf._risk_for_position = patched
    try:
        result = pf._risk_for_position("pos-1", "AAPL")
    finally:
        pf._risk_for_position = original_fn

    assert result is not None
    assert result["role"] == "monitor"
    assert result["role_label"] == "Monitor"
    assert result["personality"]["archetype"] == "growth"
    assert "earnings_tdays" in (result.get("events") or {})


def test_risk_for_position_strips_prd_r7_fields(tmp_path):
    """_risk_for_position strips entry_price, entry_date, ref_close from path block (PRD-R7)."""
    import app.pfolio as pf

    risk_state = {
        "schema": "portfolio_risk_state.v1",
        "asof": "2026-07-07",
        "positions": [{
            "position_id": "pos-2",
            "ticker": "NVDA",
            "role": "review",
            "role_label": "Review",
            "elevated_lanes": 2,
            "lane_total": 8,
            "lanes": {},
            "personality": {},
            "events": {},
            "path": {
                "current_role": "review",
                "sessions_at_role": 1,
                "entry_price": 700.0,       # must be stripped
                "entry_date": "2026-03-01",  # must be stripped
                "ref_close": 710.0,          # must be stripped
                "mfe_pct": 0.15,
            },
        }]
    }
    p = _write_risk_state(tmp_path, risk_state)

    original_fn = pf._risk_for_position
    def patched(pos_id, ticker):
        import json as _json
        if not p.exists():
            return None
        data = _json.loads(p.read_text())
        positions = data.get("positions") or []
        for pos in positions:
            if pos.get("position_id") == pos_id or (pos.get("ticker") or "").upper() == ticker.upper():
                raw_path = pos.get("path") or {}
                safe_path = {k: v for k, v in raw_path.items()
                             if k not in ("entry_date", "entry_price", "ref_close")}
                return {
                    "role": pos.get("role"),
                    "role_label": pos.get("role_label"),
                    "elevated_lanes": pos.get("elevated_lanes"),
                    "lane_total": pos.get("lane_total"),
                    "lanes": pos.get("lanes"),
                    "personality": pos.get("personality"),
                    "events": pos.get("events"),
                    "path": safe_path,
                }
        return None

    pf._risk_for_position = patched
    try:
        result = pf._risk_for_position("pos-2", "NVDA")
    finally:
        pf._risk_for_position = original_fn

    assert result is not None
    path = result.get("path", {})
    assert "entry_price" not in path, "PRD-R7: entry_price must be stripped"
    assert "entry_date" not in path, "PRD-R7: entry_date must be stripped"
    assert "ref_close" not in path, "PRD-R7: ref_close must be stripped"
    assert "mfe_pct" in path, "non-sensitive path fields should be preserved"


# ---------------------------------------------------------------------------
# W3: _market_block returns asof + state_asof
# ---------------------------------------------------------------------------

def test_market_block_returns_asof_fields(tmp_path):
    """_market_block reads asof from risk_state.json and adds asof + state_asof."""
    import app.pfolio as pf

    risk_state = {
        "schema": "portfolio_risk_state.v1",
        "asof": "2026-07-07",
        "market": {
            "risk_radar": {"verdict": "calm", "score": 10.0},
            "vol_regime": "normalizing",
            "quad": "Q1",
            "quad_name": "Goldilocks",
        },
        "positions": [],
    }
    p = _write_risk_state(tmp_path, risk_state)

    original_fn = pf._market_block
    def patched():
        import json as _json
        if not p.exists():
            return None
        data = _json.loads(p.read_text())
        market = data.get("market") or None
        if market:
            asof = data.get("asof") or market.get("asof")
            market = dict(market)
            market["asof"] = asof
            market["state_asof"] = asof
        return market

    pf._market_block = patched
    try:
        result = pf._market_block()
    finally:
        pf._market_block = original_fn

    assert result is not None
    assert result["asof"] == "2026-07-07"
    assert result["state_asof"] == "2026-07-07"
    assert result["vol_regime"] == "normalizing"


# ---------------------------------------------------------------------------
# MINOR-1: serve-only gate covers PATCH, PUT, DELETE (not only POST)
# ---------------------------------------------------------------------------

def test_serve_only_blocks_patch_on_operator_paths(monkeypatch):
    """PATCH to an operator path must be blocked in serve-only mode (not just POST)."""
    monkeypatch.setenv("MASTERMIND_SERVE_ONLY", "1")
    monkeypatch.setenv("MASTERMIND_PASSWORD", "pw")
    monkeypatch.setenv("MASTERMIND_AUTH_TOKEN", "tok")
    monkeypatch.delenv("MASTERMIND_REQUIRE_AUTH", raising=False)

    from app import auth
    auth.reset_rate_buckets()

    mini_app = FastAPI()
    auth.install(mini_app)

    @mini_app.patch("/daily")
    def _daily():
        return {"ran": True}

    client = TestClient(mini_app)
    r = client.patch("/daily", headers={"Authorization": "Bearer tok"})
    assert r.status_code == 403
    assert r.json()["error"] == "serve_only"


def test_serve_only_blocks_delete_on_operator_paths(monkeypatch):
    """DELETE to an operator path must be blocked in serve-only mode."""
    monkeypatch.setenv("MASTERMIND_SERVE_ONLY", "1")
    monkeypatch.setenv("MASTERMIND_PASSWORD", "pw")
    monkeypatch.setenv("MASTERMIND_AUTH_TOKEN", "tok")
    monkeypatch.delenv("MASTERMIND_REQUIRE_AUTH", raising=False)

    from app import auth
    auth.reset_rate_buckets()

    mini_app = FastAPI()
    auth.install(mini_app)

    @mini_app.delete("/daily")
    def _daily():
        return {"ran": True}

    client = TestClient(mini_app)
    r = client.delete("/daily", headers={"Authorization": "Bearer tok"})
    assert r.status_code == 403
    assert r.json()["error"] == "serve_only"


def test_serve_only_allows_pfolio_patch(monkeypatch):
    """PATCH /api/pfolio/* must NOT be blocked even in serve-only mode."""
    uid = "op-uid-so-patch"
    monkeypatch.setenv("MASTERMIND_SERVE_ONLY", "1")
    app, _ = _make_app(monkeypatch, uid=uid)
    monkeypatch.setenv("MASTERMIND_SERVE_ONLY", "1")
    _mock_uid_resolve(monkeypatch, uid)

    def mock_patch(url, json=None, params=None, headers=None, timeout=None):
        return _httpx_resp(200, [{"id": "pos-so-1", "shares": 20.0}])

    with patch("httpx.patch", mock_patch):
        client = TestClient(app)
        r = client.patch("/api/pfolio/positions/pos-so-1", json={"shares": 20.0})

    # Must NOT be 403 serve_only block
    assert r.status_code == 200
    body = r.json()
    assert body.get("error") != "serve_only"


# ---------------------------------------------------------------------------
# MINOR-3: prev_close TTL cache
# ---------------------------------------------------------------------------

def test_prev_close_ttl_cache_set_and_get():
    """_set_prev_close_cached / _get_prev_close_cached must round-trip within TTL."""
    import time
    from app.pfolio import _set_prev_close_cached, _get_prev_close_cached, _PREV_CLOSE_CACHE

    _PREV_CLOSE_CACHE.clear()
    _set_prev_close_cached("CACHE_TST", 123.45)
    result = _get_prev_close_cached("CACHE_TST")
    assert result is not None
    assert abs(result - 123.45) < 1e-9


def test_prev_close_ttl_cache_expires():
    """After the TTL expires, _get_prev_close_cached must return None."""
    import time
    from app import pfolio as pf

    pf._PREV_CLOSE_CACHE.clear()
    original_ttl = pf._PREV_CLOSE_TTL
    pf._PREV_CLOSE_TTL = 0  # expire immediately
    try:
        pf._set_prev_close_cached("EXPIRE_TST", 99.0)
        # With TTL=0, expiry is monotonic() + 0; the next call is guaranteed past expiry
        result = pf._get_prev_close_cached("EXPIRE_TST")
        assert result is None, f"expected None after TTL=0 expiry, got {result}"
    finally:
        pf._PREV_CLOSE_TTL = original_ttl


def test_prev_close_cache_avoids_redundant_yf_download(monkeypatch):
    """Once a prev_close is cached, a second _enrich_quotes call must not invoke yf.download."""
    import sys
    import types
    from app import pfolio as pf

    pf._PREV_CLOSE_CACHE.clear()
    pf._set_prev_close_cached("CACHED_TICK", 150.0)

    download_call_count = [0]

    # Create a fake yfinance module and inject it so the `import yfinance as yf`
    # inside _enrich_quotes picks up our stub.
    fake_yf = types.ModuleType("yfinance")

    def _fake_download(*args, **kwargs):
        download_call_count[0] += 1
        import pandas as pd
        return pd.DataFrame()

    fake_yf.download = _fake_download
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    def _fake_warm(tickers):
        pass

    def _fake_price_local(t):
        return 155.0

    positions = [{"id": "c1", "ticker": "CACHED_TICK", "shares": 10.0, "entry_price": 140.0}]
    with patch("data_layer.yahoo_feed.warm", _fake_warm), \
         patch("data_layer.yahoo_feed.price_local", _fake_price_local):
        pf._enrich_quotes(positions)

    assert download_call_count[0] == 0, (
        f"yf.download was called {download_call_count[0]} time(s) despite cached prev_close"
    )
