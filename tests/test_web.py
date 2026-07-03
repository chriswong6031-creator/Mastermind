"""Dashboard web route tests.

Uses TestClient without the context manager to avoid firing the scheduler startup
event. Relies on real data fixtures already in data/ (portfolio, research, quiver).
"""
from __future__ import annotations

from fastapi.testclient import TestClient


def _client():
    from app.main import app
    return TestClient(app, raise_server_exceptions=True)


def test_dashboard_serves_html():
    client = _client()
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
    assert "MASTERMIND" in r.text


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


def test_api_competitors_returns_strategies():
    client = _client()
    r = client.get("/api/competitors")
    assert r.status_code == 200
    data = r.json()
    assert "strategies" in data
    assert isinstance(data["strategies"], list)
    assert len(data["strategies"]) > 0
