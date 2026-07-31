"""Dashboard web route tests.

Uses TestClient without the context manager to avoid firing the scheduler startup
event. Relies on real data fixtures already in data/ (portfolio, research).
"""
from __future__ import annotations

from fastapi.testclient import TestClient

# ── W8 legacy-contract pin (2026-07-19): this file tests pre-W8 mechanics (design
# research/FLAGSHIP_V2_DECISION_CORE.md). The v2 entry/context gates + feeds are exercised by
# tests/test_flagship_v2_replay.py + tests/test_entry_context_engines.py; here they are pinned
# OFF so the legacy contracts stay deterministic under a live vendor checkout.
import pytest as _pytest_w8


@_pytest_w8.fixture(autouse=True)
def _w8_legacy_env(monkeypatch):
    monkeypatch.setenv("MASTERMIND_ENTRY_GATE", "0")
    monkeypatch.setenv("MASTERMIND_PROPHET_FEED", "0")
    monkeypatch.setenv("MASTERMIND_ROTATION_IN", "off")
    monkeypatch.setenv("MASTERMIND_NW_DECISION", "off")
    try:
        from portfolio import prophet_feed as _pf
        _pf._reset_cache()
    except Exception:
        pass
    yield
    try:
        from portfolio import prophet_feed as _pf
        _pf._reset_cache()
    except Exception:
        pass



def _client():
    from app.main import app
    return TestClient(app, raise_server_exceptions=True)


def test_dashboard_serves_html():
    client = _client()
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
    assert "MASTERMIND" in r.text


def test_account_script_serves_javascript():
    client = _client()
    r = client.get("/account.js")
    assert r.status_code == 200
    assert "application/javascript" in r.headers.get("content-type", "")
    assert "Mastermind user profile" in r.text


def test_static_shells_and_assets_are_short_lived_cacheable():
    client = _client()
    for path in ("/", "/research", "/desk", "/self", "/portfolio_desk",
                 "/market_view", "/agenda"):
        r = client.get(path)
        assert r.status_code == 200
        cache = r.headers.get("cache-control", "")
        assert "public" in cache
        assert "max-age=120" in cache
        assert "stale-while-revalidate=600" in cache

    for path in ("/theme.css", "/theme.js", "/chat.js", "/account.js"):
        r = client.get(path)
        assert r.status_code == 200
        cache = r.headers.get("cache-control", "")
        assert "public" in cache
        assert "max-age=300" in cache
        assert "stale-while-revalidate=3600" in cache


def test_read_api_cache_allows_brief_browser_and_edge_reuse(monkeypatch):
    from app import response_cache

    assert "/api/account" in response_cache._DENY_PREFIXES
    assert "/api/live_marks" in response_cache._DENY_PREFIXES
    monkeypatch.setenv("MASTERMIND_RESP_CACHE_TTL", "30")
    response_cache.clear()
    r = _client().get("/api/portfolios")
    assert r.status_code == 200
    assert r.headers.get("x-mm-cache") == "miss"
    cache = r.headers.get("cache-control", "")
    assert "max-age=5" in cache
    assert "stale-while-revalidate=5" in cache
    response_cache.clear()


def test_live_marks_contract_is_never_browser_cached(monkeypatch):
    from portfolio import market_sessions

    monkeypatch.setattr(market_sessions, "status_for_portfolio", lambda portfolio_id: {
        "venue": "US", "market": "NYSE", "timezone": "America/New_York",
        "is_open": False, "state": "post_close", "trading_day": True,
        "holiday": False, "as_of": "2026-07-30T17:00:00-04:00",
        "next_open": "2026-07-31T09:30:00-04:00", "poll_after_seconds": 59405,
    })
    r = _client().get("/api/live_marks?portfolio=flagship")
    assert r.status_code == 200
    data = r.json()
    assert data["schema_version"] == "live_marks.v1"
    assert data["portfolio"] == "flagship"
    assert data["session"]["is_open"] is False
    assert data["poll_after_seconds"] == 59405
    assert "current_nav" in data["performance"]
    assert r.headers["cache-control"] == "no-store"


def test_live_marks_only_warms_feed_during_exchange_session(monkeypatch):
    from app import web
    from data_layer import yahoo_feed
    from portfolio import market_sessions

    calls = []
    monkeypatch.setattr(web, "_account_tickers", lambda portfolio_id: ["600000.SS"])
    monkeypatch.setattr(web, "_book_marks", lambda portfolio_id, refresh=False: {})
    monkeypatch.setattr(yahoo_feed, "warm", lambda tickers: calls.append(list(tickers)))
    closed = {
        "venue": "CN", "market": "SSE/SZSE", "timezone": "Asia/Shanghai",
        "is_open": False, "state": "lunch_break", "trading_day": True,
        "holiday": False, "as_of": "2026-07-30T12:00:00+08:00",
        "next_open": "2026-07-30T13:00:00+08:00", "poll_after_seconds": 3605,
    }
    monkeypatch.setattr(market_sessions, "status_for_portfolio", lambda portfolio_id: closed)
    assert web.api_live_marks("china").status_code == 200
    assert calls == []

    monkeypatch.setattr(
        market_sessions, "status_for_portfolio",
        lambda portfolio_id: {**closed, "is_open": True, "state": "open",
                              "poll_after_seconds": 120})
    assert web.api_live_marks("china").status_code == 200
    assert calls == [["600000.SS"]]


def test_dashboard_live_cache_converts_a_share_cny_to_usd(monkeypatch):
    from app import web
    from data_layer import terminal_prices, yahoo_feed
    from portfolio import fx

    monkeypatch.setattr(yahoo_feed, "price_cached", lambda ticker: 72.0)
    monkeypatch.setattr(terminal_prices, "price_usd", lambda ticker: None)
    monkeypatch.setattr(fx, "to_usd", lambda local, ticker: local / 7.2)
    assert web._dash_mark_usd("600000.SS") == 10.0


def test_market_view_page_serves_html():
    """The E1.2 mirror page (/market_view) is a standalone static page that fetches the artifact
    client-side. Intent-only: assert it serves HTML with the render root, never a market state."""
    client = _client()
    r = client.get("/market_view")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
    assert "mv-root" in r.text          # the client-side render target
    assert "/api/market_view" in r.text  # fetches the artifact endpoint


def test_api_market_view_serves_artifact_or_honest_stub():
    """The E1.2 data endpoint serves data/market_view/latest.json read-only. Intent-only: either a
    well-formed market_view.v1 artifact (schema + planes) OR an honest available:false stub — never a
    500, and never a pinned market state."""
    client = _client()
    r = client.get("/api/market_view")
    assert r.status_code in (200, 404)  # 404 = honest 'not built yet' stub
    data = r.json()
    if r.status_code == 200:
        assert data.get("schema_version") == "market_view.v1"
        assert isinstance(data.get("planes"), dict)
        assert "label_vs_planes" in data
    else:
        assert data.get("available") is False


def test_api_portfolio_schema():
    client = _client()
    r = client.get("/api/portfolio")
    assert r.status_code == 200
    data = r.json()
    assert data.get("schema") == "portfolio.v1"
    positions = data.get("positions", [])
    assert isinstance(positions, list) and positions, "positions should be a non-empty list"
    for p in positions:
        assert "ticker" in p and "weight" in p, f"position missing ticker/weight: {p}"
    # Don't pin a specific holding (the book rotates) — but a leadership-sleeve
    # ETF like SMH should be present while it tops the RS ranks.
    tickers = [p["ticker"] for p in positions]
    assert "SMH" in tickers, f"SMH not found in positions: {tickers}"


def test_api_research_returns_list():
    client = _client()
    r = client.get("/api/research")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
