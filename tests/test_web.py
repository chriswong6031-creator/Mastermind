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
