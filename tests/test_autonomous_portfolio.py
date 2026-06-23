"""Autonomous portfolio + multi-portfolio store contracts.

Covers the new free-form Opus-Brain book and the portfolio-id parameterization:
registry path resolution, store isolation from the flagship book, the submit_book
tool's validation, the run_autonomous build (offline + with a simulated submission),
and the portfolio-aware web endpoints.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from portfolio import paper_account, position_log, registry, trade_history


@pytest.fixture
def iso(tmp_path, monkeypatch):
    """Isolate all NON-legacy (per-id) portfolio state to a tmp root.

    registry.data_dir() derives per-id dirs off registry._ROOT, so patching it
    redirects the autonomous book's account / fills / nav / ledger / decisions / bridge.
    """
    monkeypatch.setattr(registry, "_ROOT", tmp_path, raising=False)
    return tmp_path


def test_registry_paths(iso):
    assert registry.DEFAULT_ID == "flagship"
    assert "autonomous" in registry.ids()
    # flagship / None resolve to the legacy dir; others to data/portfolios/<id>
    assert registry.data_dir("flagship").name == "portfolio"
    assert registry.data_dir(None).name == "portfolio"
    assert registry.data_dir("autonomous") == iso / "data" / "portfolios" / "autonomous"
    assert registry.starting_nav("autonomous") == 1_000_000.0


def test_autonomous_store_isolated_from_flagship(iso, monkeypatch):
    monkeypatch.setattr(paper_account, "_current_price",
                        lambda t: {"AAPL": 200.0, "SPY": 740.0}.get(t))
    # a buy on the autonomous book lands under the per-id dir...
    r = paper_account.execute_fill("AAPL", "buy", shares=10, price=200.0,
                                   asof="2026-06-21", portfolio_id="autonomous")
    assert r["ok"] and r["shares"] == 10
    auto_dir = registry.data_dir("autonomous")
    assert (auto_dir / "account.json").exists()
    acct = json.loads((auto_dir / "account.json").read_text())
    assert "AAPL" in acct["positions"]
    # ...and the legacy/flagship store (which uses the module-global paths, NOT the tmp
    # per-id dir) is a different location entirely — no autonomous file leaks into it.
    assert paper_account._paths(None)["account"] != (auto_dir / "account.json")
    # per-id blotter + ledger also resolve under the tmp dir
    assert trade_history._fills_path("autonomous") == auto_dir / "fills.jsonl"
    assert position_log._ledger_path("autonomous") == auto_dir / "positions_ledger.json"


def test_submit_book_scales_and_dedups(iso):
    res = asyncio.run(autonomous_submit({
        "holdings": [
            {"ticker": "AAA", "weight": 0.8, "rationale": "a"},
            {"ticker": "BBB", "weight": 0.8, "rationale": "b"},
            {"ticker": "CCC", "weight": 0.2, "rationale": ""},     # dropped: no rationale
            {"ticker": "AAA", "weight": 0.1, "rationale": "dup"},  # dropped: duplicate
        ],
        "summary": "test book",
    }))
    from brain import autonomous_mcp
    sub = autonomous_mcp.read_submission()
    assert {h["ticker"] for h in sub["holdings"]} == {"AAA", "BBB"}
    assert sub["scaled_to_no_leverage"] is True
    assert sub["gross"] == pytest.approx(1.0)           # 0.8+0.8 scaled down to 1.0
    assert all(h["rationale"] for h in sub["holdings"])  # rationale required


def test_run_autonomous_offline_inaugural(iso, monkeypatch):
    monkeypatch.setattr(paper_account, "_current_price", lambda t: {"SPY": 740.0}.get(t))
    from bot import autonomous
    out = autonomous.run_autonomous(asof="2026-06-21", armed=False)
    assert out["inaugural"] is True
    assert out["decided"] is False
    assert out["nav"] == 1_000_000.0
    # publishes a portfolio.v1 contract for the autonomous book
    latest = json.loads((registry.data_dir("autonomous") / "latest.json").read_text())
    assert latest["portfolio_id"] == "autonomous"
    assert latest["schema"] == "portfolio.v1"


def test_run_autonomous_executes_submission(iso, monkeypatch):
    prices = {"NVDA": 210.0, "MSFT": 380.0, "SPY": 740.0}   # GLD deliberately unpriceable
    monkeypatch.setattr(paper_account, "_current_price", lambda t: prices.get(t))
    from bot import autonomous, settle
    monkeypatch.setattr(settle, "is_open", lambda pid: True)   # force OPEN so it fills (not queues)
    from brain import autonomous_mcp

    def fake_brain(asof, inaugural):
        autonomous_mcp.submission_path().parent.mkdir(parents=True, exist_ok=True)
        autonomous_mcp.submission_path().write_text(json.dumps({
            "holdings": [
                {"ticker": "NVDA", "weight": 0.4, "rationale": "AI leadership"},
                {"ticker": "MSFT", "weight": 0.3, "rationale": "cloud + AI"},
                {"ticker": "GLD", "weight": 0.1, "rationale": "hedge"},
            ],
            "summary": "barbell", "gross": 0.8,
        }))
        return {"ok": True, "text": "x", "cost_usd": 0.0, "model": "claude-opus-4-8"}

    monkeypatch.setattr(autonomous, "_run_brain", fake_brain)
    out = autonomous.run_autonomous(asof="2026-06-21", armed=True)
    assert out["decided"] is True
    assert "GLD" in out["skipped_unpriceable"]            # unpriceable name skipped, surfaced
    sides = {(t["ticker"], t["side"]) for t in out["executed"]}
    assert ("NVDA", "buy") in sides and ("MSFT", "buy") in sides
    # decision log captured the day's narrative + per-name rationale
    decs = autonomous.load_decisions()
    assert decs and decs[0]["summary"] == "barbell"
    assert any(h["ticker"] == "NVDA" and h["rationale"] for h in decs[0]["holdings"])
    # positions carry the rationale for the dashboard
    latest = json.loads((registry.data_dir("autonomous") / "latest.json").read_text())
    nvda = next(p for p in latest["positions"] if p["ticker"] == "NVDA")
    assert nvda["rationale"] and nvda["sleeve"] == "brain"


def test_web_endpoints_portfolio_aware(iso, monkeypatch):
    monkeypatch.setattr(paper_account, "_current_price", lambda t: {"SPY": 740.0}.get(t))
    from bot import autonomous
    autonomous.run_autonomous(asof="2026-06-21", armed=False)   # seed the autonomous book

    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app, raise_server_exceptions=True)

    portfolios = c.get("/api/portfolios").json()
    ids = {p["id"] for p in portfolios["portfolios"]}
    assert {"flagship", "autonomous"} <= ids
    assert portfolios["default"] == "flagship"

    dec = c.get("/api/decisions?portfolio=autonomous").json()
    assert "decisions" in dec
    # flagship has no free-form decision log
    assert c.get("/api/decisions?portfolio=flagship").json()["decisions"] == []

    perf = c.get("/api/performance?portfolio=autonomous").json()
    assert perf["starting_nav"] == 1_000_000

    book = c.get("/api/portfolio?portfolio=autonomous").json()
    assert book["portfolio_id"] == "autonomous"


# --- helper: invoke the SdkMcpTool's async handler directly ---------------------
def autonomous_submit(args):
    from brain import autonomous_mcp
    return autonomous_mcp.submit_book.handler(args)
