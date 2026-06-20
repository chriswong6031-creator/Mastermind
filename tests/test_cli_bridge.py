"""Tests for the Claude Code reasoning bridge.

Pure-logic tests always run. A real round-trip is opt-in (BOT_TEST_LIVE_LLM=1) so the
default suite never spends subscription tokens.
"""
import os

import pytest

import bot  # noqa: F401

from brain import cli_bridge, client


def test_model_tier_routing():
    # the in-house policy: Opus=deep, Sonnet=medium/code, Haiku=high-volume
    assert cli_bridge.resolve_model("pm") == "opus"
    assert cli_bridge.resolve_model("deep") == "opus"
    assert cli_bridge.resolve_model("analyst") == "sonnet"
    assert cli_bridge.resolve_model("scout") == "haiku"
    assert cli_bridge.resolve_model("fable") == "fable"
    # explicit model wins over role
    assert cli_bridge.resolve_model("scout", model="claude-opus-4-8") == "claude-opus-4-8"


def test_backend_resolution(monkeypatch):
    monkeypatch.delenv("BOT_LLM_BACKEND", raising=False)
    assert client.backend() == "cli"                      # default from config/agents.yml
    monkeypatch.setenv("BOT_LLM_BACKEND", "api")
    assert client.backend() == "api"


def test_config_has_readonly_tools():
    rc = cli_bridge._cfg()["reasoning"]
    assert "Read" in rc["allowed_tools"]
    assert not (set(rc["allowed_tools"]) & {"Write", "Edit", "Bash"})   # read-only reasoning layer


def test_reason_graceful_without_cli(monkeypatch):
    # if the CLI binary isn't found, reason() returns a structured failure, never raises
    monkeypatch.setattr(cli_bridge, "cli_path", lambda: None)
    import asyncio
    out = asyncio.run(cli_bridge.reason("hi", role="scout"))
    assert out["ok"] is False and out["backend"] == "none" and out["model"] == "haiku"


@pytest.mark.skipif(not os.environ.get("BOT_TEST_LIVE_LLM"), reason="set BOT_TEST_LIVE_LLM=1 for a real round-trip")
def test_live_round_trip():
    assert cli_bridge.available()
    out = cli_bridge.reason_sync("Reply with exactly: PONG", role="scout", max_turns=1)
    assert out["ok"] and "PONG" in (out["text"] or "")
