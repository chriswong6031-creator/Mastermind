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
