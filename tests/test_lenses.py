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


# ---------------- alt-data flow lens (Quiver / TrumpFlow) ----------------
def test_altdata_lens_surfaces_even_without_stockdata(monkeypatch):
    bt = {"tickers": {"EFX": {"convergence_score": 2, "channels": ["gov_contract", "trump"],
                              "trump_linked": True, "gov_contract_usd_30d": 7e7, "trump_side": "buy"}}}
    monkeypatch.setattr(lenses, "_load", lambda rel: bt if "altdata/by_ticker" in rel else None)
    rows = {r["lens"]: r for r in lenses.decision_matrix("EFX", "name")["rows"]}
    assert "altdata_flow" in rows                                   # present even with no S&P stockdata
    r = rows["altdata_flow"]
    assert r["status"] == "context" and r["direction"] == "bull"    # never validated; buy-side flow
    assert r["value"]["convergence_score"] == 2 and r["value"]["trump_linked"] is True
    assert "convergence" in r["note"]


def test_altdata_divergence_patterns():
    trap = lenses._divergences([{"lens": "altdata_flow", "direction": "bull", "value": {}},
                                {"lens": "extension", "direction": "bear", "value": {}}])
    assert any(x["pattern"] == "political_crowd_trap" for x in trap)
    early = lenses._divergences([{"lens": "altdata_flow", "direction": "bull", "value": {}},
                                 {"lens": "valuation", "direction": "neutral", "value": {}}])
    assert any(x["pattern"] == "political_flow_early" for x in early)


def test_get_altdata_tool(monkeypatch):
    bt = {"tickers": {"EFX": {"convergence_score": 2, "channels": ["gov_contract", "trump"], "trump_linked": True}}}
    latent = {"watch": [{"ticker": "HUT", "themes": [{"en": "AI power & data-center infrastructure"}],
                         "trump_people": ["Eric Trump"], "top_holder": {"owner": "BlackRock"}}],
              "mismatches": [{"entity_ticker": "ABTC", "repointed_ticker": "HUT", "real_theme": {"en": "AI infra"}}]}
    monkeypatch.setattr(bot_mcp, "_read_json",
                        lambda p: bt if "by_ticker" in str(p) else latent if "latent" in str(p) else None)
    d = json.loads(asyncio.run(bot_mcp.get_altdata.handler({"ticker": "EFX"}))["content"][0]["text"])
    assert d["ticker"] == "EFX" and d["flow"]["convergence_score"] == 2
    # HUT carries no direct flow row but resolves the latent graph + the ABTC->HUT label mismatch
    d2 = json.loads(asyncio.run(bot_mcp.get_altdata.handler({"ticker": "HUT"}))["content"][0]["text"])
    assert d2["latent_graph"]["in_graph"] is True and d2["label_mismatch"]["repointed_ticker"] == "HUT"
    assert "mcp__bot__get_altdata" in bot_mcp.TOOL_NAMES
