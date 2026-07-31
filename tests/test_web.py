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
    monkeypatch.setenv("MASTERMIND_RESP_CACHE_TTL", "30")
    response_cache.clear()
    r = _client().get("/api/portfolios")
    assert r.status_code == 200
    assert r.headers.get("x-mm-cache") == "miss"
    cache = r.headers.get("cache-control", "")
    assert "max-age=5" in cache
    assert "stale-while-revalidate=5" in cache
    response_cache.clear()


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
