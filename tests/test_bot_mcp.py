"""Tests for the bot's MCP tool surface — the read + write-back armament for Claude.

These exercise the tools DIRECTLY (no Claude auth needed), proving the armament works:
Claude's reads return real dashboard data and its actions write to the app's review queue.
"""
import asyncio
import json
from pathlib import Path

import bot  # noqa: F401

from brain import bot_mcp, cli_bridge

_ROOT = Path(__file__).resolve().parent.parent


def _text(result: dict) -> str:
    return result["content"][0]["text"]


def test_read_tools_return_real_data():
    reg = json.loads(_text(asyncio.run(bot_mcp.get_regime.handler({}))))
    assert reg["quad"] in {"Q1", "Q2", "Q3", "Q4"}          # live regime
    themes = json.loads(_text(asyncio.run(bot_mcp.get_themes.handler({"region": "us"}))))
    assert isinstance(themes["themes"], list) and themes["themes"]


def test_read_signal_is_allowlisted():
    denied = _text(asyncio.run(bot_mcp.read_signal.handler({"path": "/etc/passwd"})))
    assert "DENIED" in denied


def test_action_tools_write_to_review_queue(tmp_path, monkeypatch):
    # isolate writes to a temp dir — the action handlers persist real files, and
    # without this the suite would spam stub notes/proposals into the live data feed.
    monkeypatch.setattr(bot_mcp, "_ROOT", tmp_path)
    monkeypatch.setattr(bot_mcp, "_RESEARCH", tmp_path / "data" / "research")
    monkeypatch.setattr(bot_mcp, "_PROPOSALS", tmp_path / "data" / "brain" / "proposals.jsonl")

    note = _text(asyncio.run(bot_mcp.save_research_note.handler(
        {"title": "AI power bottleneck", "body": "Compute is migrating to electricity.",
         "tickers": ["NVDA", "VST"]})))
    assert "research note" in note and (tmp_path / "data" / "research" / "notes").exists()

    prop = _text(asyncio.run(bot_mcp.propose_thesis.handler(
        {"subject": "VST", "lean": "add", "conviction": "medium",
         "thesis": "Power demand from AI data centers", "horizon_d": 60})))
    assert "review queue" in prop and "NOT executed" in prop
    rows = (tmp_path / "data" / "brain" / "proposals.jsonl").read_text().strip().splitlines()
    assert json.loads(rows[-1])["subject"] == "VST"


def test_server_and_allowlist_build():
    srv = bot_mcp.build_server()
    assert srv is not None
    allowed = bot_mcp.armed_allowed_tools()
    assert "mcp__bot__get_regime" in allowed and "WebSearch" in allowed
    assert "mcp__bot__propose_thesis" in allowed


def test_subscription_env_strips_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    env = cli_bridge._subscription_env()
    assert "ANTHROPIC_API_KEY" not in env                   # subscription, not metered API


def test_intake_tools_registered_and_callable():
    # the Phase-6 intake / transmission tools must be armed
    allowed = bot_mcp.armed_allowed_tools()
    for name in ("get_daily_briefing", "get_intake_candidates", "get_ticker_package"):
        assert f"mcp__bot__{name}" in allowed

    # get_daily_briefing — macro frame + ranked queue (composes live from the intake funnel
    # when briefing.json isn't vendored yet; must never error)
    brief = json.loads(_text(asyncio.run(bot_mcp.get_daily_briefing.handler({"top": 5}))))
    assert "macro_context" in brief
    assert "priority_queue" in brief and isinstance(brief["priority_queue"], list)

    # get_intake_candidates — unified queue with provenance; tiers split on request
    cand = json.loads(_text(asyncio.run(bot_mcp.get_intake_candidates.handler({"limit": 8, "tiers": True}))))
    assert "candidates" in cand and isinstance(cand["candidates"], list)
    assert "salience" in cand and {"act", "watch", "divergent"} <= set(cand["salience"])
    for c in cand["candidates"]:
        assert "ticker" in c and "score" in c and "sources" in c    # provenance present


def test_ticker_package_degrades_cleanly():
    # a name with no dashboard coverage degrades to a plain message, never raises
    out = _text(asyncio.run(bot_mcp.get_ticker_package.handler({"ticker": "ZZZZ"})))
    assert "ZZZZ" in out


def test_intel_hub_tool(tmp_path, monkeypatch):
    # armed + degrades cleanly when hub.json absent
    assert "mcp__bot__get_intel_hub" in bot_mcp.armed_allowed_tools()
    monkeypatch.setattr(bot_mcp, "_V", tmp_path / "novendor")
    assert "not built yet" in _text(asyncio.run(bot_mcp.get_intel_hub.handler({})))

    # with a hub.json present: command pull (no ticker) + single-dossier pull (ticker)
    hub = {"as_of": "2026-06-21", "macro_context": {"regime": "Goldilocks"},
           "desks": {"policy": {"live": True}}, "counts": {"theme_wide": 3},
           "n_actionable": 2, "divergence_alerts": {"early_edge": [], "crowded_top": []},
           "sector_heat": [{"etf": "XLK", "mean_conviction": 31.6}],
           "how_to_use": "read macro first",
           "command": [{"ticker": "NVDA", "name": "Nvidia", "composite_conviction": 100,
                        "lean": 1, "n_confirm": 5, "flags": ["confirmed_trend", "theme_wide"],
                        "read": "5 desks agree", "peers": ["DELL", "MSFT"], "falsifier": None}]}
    site = tmp_path / "novendor" / "site" / "intel_hub"
    site.mkdir(parents=True)
    (site / "hub.json").write_text(json.dumps(hub))

    full = json.loads(_text(asyncio.run(bot_mcp.get_intel_hub.handler({"top": 5}))))
    assert full["macro_context"]["regime"] == "Goldilocks"
    assert full["command"][0]["ticker"] == "NVDA" and "theme_wide" in full["command"][0]["flags"]
    assert "sector_heat" in full and "counts" in full

    one = json.loads(_text(asyncio.run(bot_mcp.get_intel_hub.handler({"ticker": "nvda"}))))
    assert one["ticker"] == "NVDA" and one["composite_conviction"] == 100
    assert "ZZZZ not in" in _text(asyncio.run(bot_mcp.get_intel_hub.handler({"ticker": "ZZZZ"})))
