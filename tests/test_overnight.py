"""The live overnight tape + the overnight watch loop.

Covers data_layer.overnight (the live cross-asset tape + the distilled risk read + the deterministic
tripwire) and bot.overnight (the watch tick: no-op when calm / open / unqueued; fires the Brain
re-decision only on a MATERIAL overnight move), plus the get_overnight_tape MCP tool.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from portfolio import paper_account, registry


@pytest.fixture
def iso(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "_ROOT", tmp_path, raising=False)
    return tmp_path


# --------------------------------------------------------------------------- the tape

def test_risk_read_classification():
    from data_layer import overnight
    calm = {"us_futures": [{"ticker": "ES=F", "change_pct": 0.2}],
            "international": [{"ticker": "^N225", "change_pct": 0.3}],
            "vol": [{"ticker": "^VIX", "change_pct": -1.0}]}
    assert overnight.risk_read(calm)["state"] == "calm"
    stressed = {"us_futures": [{"ticker": "ES=F", "change_pct": -2.0}],
                "international": [], "vol": [{"ticker": "^VIX", "change_pct": 15.0}]}
    assert overnight.risk_read(stressed)["state"] == "stressed"
    elevated = {"us_futures": [{"ticker": "ES=F", "change_pct": -0.8}], "international": [], "vol": []}
    assert overnight.risk_read(elevated)["state"] == "elevated"


def test_is_material_tripwire():
    from data_layer import overnight
    assert overnight.is_material({"risk": {"state": "stressed"}}) is True
    assert overnight.is_material({"risk": {"state": "elevated"}}) is True
    assert overnight.is_material({"risk": {"state": "calm"}}) is False


def test_tape_degrades_offline():
    # conftest stubs yfinance off → the tape degrades to empty groups + a valid calm read, never raises
    from data_layer import overnight
    overnight.clear_cache()
    t = overnight.tape(force=True)
    assert t["live"] is False
    assert isinstance(t["groups"], dict) and "us_futures" in t["groups"]
    assert t["risk"]["state"] == "calm"
    assert "Overnight tape" in overnight.brief(t)


def test_get_overnight_tape_tool(monkeypatch):
    from brain import bot_mcp
    from data_layer import overnight as ov
    monkeypatch.setattr(ov, "tape", lambda force=False: {"risk": {"state": "calm"}, "groups": {}, "live": True})
    out = asyncio.run(bot_mcp.get_overnight_tape.handler({}))
    payload = json.loads(out["content"][0]["text"])
    assert payload["risk"]["state"] == "calm"


# --------------------------------------------------------------------------- the watch loop

def test_watch_skips_when_market_open(iso, monkeypatch):
    from bot import overnight, settle
    monkeypatch.setattr(settle, "is_open", lambda pid: True)
    assert overnight.watch("etf")["skipped"] == "market_open"


def test_watch_skips_when_nothing_queued(iso, monkeypatch):
    from bot import overnight, settle
    monkeypatch.setattr(settle, "is_open", lambda pid: False)
    assert overnight.watch("etf")["skipped"] == "nothing_queued"


def test_watch_skips_when_tape_calm(iso, monkeypatch):
    from bot import overnight, settle
    from data_layer import overnight as ov
    monkeypatch.setattr(settle, "is_open", lambda pid: False)
    paper_account.save_pending_target({"SPY": 0.5}, "2026-06-23", portfolio_id="etf")
    monkeypatch.setattr(ov, "tape", lambda force=False: {"risk": {"state": "calm", "reasons": ["x"]}, "groups": {}})
    assert overnight.watch("etf")["skipped"] == "tape_calm"     # deterministic tripwire → no LLM fired


def test_watch_fires_on_material_move(iso, monkeypatch):
    from bot import overnight, settle, etf as etf_mod
    from data_layer import overnight as ov
    monkeypatch.setattr(settle, "is_open", lambda pid: False)
    paper_account.save_pending_target({"XLK": 0.5}, "2026-06-23", portfolio_id="etf")
    stressed = {"risk": {"state": "stressed", "reasons": ["US futures -2%"]},
                "groups": {"us_futures": [{"name": "S&P 500 fut", "change_pct": -2.0}],
                           "international": [], "vol": [], "fx_rates": []}}
    monkeypatch.setattr(ov, "tape", lambda force=False: stressed)
    captured = {}

    def fake_run(asof=None, directive=None):
        captured["directive"] = directive
        return {"decided": True, "queued_for_open": True, "holdings": 1, "brain": {"ok": True}}

    monkeypatch.setattr(etf_mod, "run_etf", fake_run)
    res = overnight.watch("etf", asof="2026-06-23")
    assert res.get("refined", {}).get("decided") is True
    assert res["refined"]["queued_for_open"] is True
    assert "OVERNIGHT REVIEW" in captured["directive"]
    assert "stressed" in captured["directive"] and "S&P 500 fut -2.00%" in captured["directive"]
