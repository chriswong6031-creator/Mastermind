"""The multi-sided decision matrix — every lens read from live data, gated by confluence/divergence."""
import asyncio
import json

import bot  # noqa: F401

from portfolio import lenses
from brain import bot_mcp


def test_matrix_reads_all_sides_live():
    rows = {r["lens"]: r for r in lenses.decision_matrix("NVDA", "name")["rows"]}
    # every side is present
    for lens in ["valuation", "quality", "growth", "asymmetry", "risk_drawdown", "extension",
                 "flows_13f", "flows_etf", "options", "conviction", "macro_risk", "fed_path", "cross_asset"]:
        assert lens in rows, lens
    # the validated risk lenses carry their tag
    assert rows["risk_drawdown"]["status"] == "validated"
    assert rows["extension"]["status"] == "validated"
    # real values came through
    assert rows["valuation"]["value"]["value_z"] is not None
    assert rows["conviction"]["value"]["band"] is not None


def test_synthesis_and_divergence_on_nvda():
    s = lenses.full("NVDA", "name")["synthesis"]
    assert "confluence" in s and -1 <= s["confluence"] <= 1
    assert s["size_authority"] in ("up", "down", "hold", "blocked")
    # NVDA: high-conviction + expensive + 13F-selling -> the distribution trap
    assert any(d["pattern"] == "distribution" for d in s["divergences"])


def test_hard_veto_caps_size():
    fake = {"rows": [
        {"lens": "extension", "direction": "bear", "value": {"parabolic": True}},
        {"lens": "valuation", "direction": "bull", "value": {}},
        {"lens": "flows_13f", "direction": "bull", "value": {}},
        {"lens": "conviction", "direction": "bull", "value": {"band": "high"}},
    ]}
    s = lenses.synthesize(fake)
    assert "parabolic" in s["vetoes"] and s["size_authority"] == "blocked"


def test_mcp_decision_matrix_tool():
    out = asyncio.run(bot_mcp.get_decision_matrix.handler({"subject": "NVDA", "kind": "name"}))
    d = json.loads(out["content"][0]["text"])
    assert d["subject"] == "NVDA" and "synthesis" in d
    assert "mcp__bot__get_decision_matrix" in bot_mcp.TOOL_NAMES
