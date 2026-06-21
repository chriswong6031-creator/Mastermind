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
    # NVDA is a cheap-for-growth leader (PEG ~0.25) — after the valuation/13F fix it must NOT be
    # caught in the 'distribution' trap (the false-reject the raw value factor + 1-name 13F margin
    # used to manufacture). See test_nvda_growth_leader_passes_gate for the full regression.
    assert not any(d["pattern"] == "distribution" for d in s["divergences"])


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


# --- AVGO/NVDA alignment: growth-adjusted valuation + 13F min-sample gate (the NVDA false-reject) ---
def test_valuation_dir_is_growth_adjusted():
    # cheap-for-growth leader (NVDA-like): expensive on the value factor but PEG<0.8 -> NOT bear
    dirv, peg = lenses._valuation_dir(-1.05, 48.0, 16.6, 66.9, -6.6)   # rev_cagr leads (eps noisy/neg)
    assert dirv == "bull" and peg is not None and peg < 0.8
    # fairly-valued-for-growth (AVGO-like): expensive factor but PEG ~1 -> neutral, not bear
    dirv2, _ = lenses._valuation_dir(-0.9, None, 21.3, 21.7, -5.5)
    assert dirv2 == "neutral"
    # genuinely expensive (no growth to justify it) -> bear
    assert lenses._valuation_dir(-1.0, 20.0, 40.0, 5.0, None)[0] == "bear"
    # cheap on the factor itself -> bull regardless of growth
    assert lenses._valuation_dir(0.6, 70.0, None, None, None)[0] == "bull"


def test_flows_13f_min_sample_margin_gate():
    assert lenses._flows_13f_dir(1, 2) == "neutral"     # 1-name margin = noise, not distribution
    assert lenses._flows_13f_dir(3, 1) == "bull"        # >=2 net buyers
    assert lenses._flows_13f_dir(1, 4) == "bear"        # >=2 net sellers
    assert lenses._flows_13f_dir(2, 2) == "neutral"
    assert lenses._flows_13f_dir(None, None) is None    # no 13F coverage


def test_nvda_growth_leader_passes_gate():
    # regression: NVDA must no longer be a tight-factor false-reject. With PEG-aware valuation and
    # the 13F gate, the 'distribution' divergence does not fire and the gate sizes it.
    f = lenses.full("NVDA")
    rows = {r["lens"]: r for r in f["rows"]}
    assert rows["valuation"]["direction"] != "bear"
    assert "distribution" not in [d["pattern"] for d in f["synthesis"]["divergences"]]
    assert f["synthesis"]["size_authority"] == "up" and not f["synthesis"]["vetoes"]


# ---------------- vol-regime context lens (subtract-only gross caution) ----------------
def _vol_mm(regime="backwardation-stress", **extra):
    return {"schema": "vol_regime.context.v1", "regime": regime, "kill_switch": regime == "backwardation-stress",
            "vol_target_scalar": 0.7, "scored_active": False, "scored_score": None,
            "ts_slope_state": "backwardation", "fragility_confluence": 3, **extra}


def test_vol_regime_lens_present_and_subtract_only(monkeypatch):
    """The vol-regime lens votes BEAR in a risk-off state and NEUTRAL when calm — it can never
    vote BULL (it is a subtract-only gross caution, not a size driver). It rides in _macro_rows
    so every name/theme matrix with macro context carries it."""
    monkeypatch.setattr(lenses, "_load",
                        lambda rel: _vol_mm() if "vol/mastermind" in rel else None)
    r = lenses._vol_regime_row()
    assert r["lens"] == "vol_regime" and r["status"] == "context" and r["direction"] == "bear"
    assert r["value"]["regime"] == "backwardation-stress" and r["value"]["vol_target_scalar"] == 0.7
    assert "vol_regime" in {row["lens"] for row in lenses._macro_rows()}   # wired into the macro rows

    monkeypatch.setattr(lenses, "_load",
                        lambda rel: _vol_mm(regime="normalizing", kill_switch=False) if "vol/mastermind" in rel else None)
    assert lenses._vol_regime_row()["direction"] == "neutral"             # calm => no vote (never bull)


def test_vol_regime_lens_missing_is_graceful(monkeypatch):
    monkeypatch.setattr(lenses, "_load", lambda rel: None)
    r = lenses._vol_regime_row()
    assert r["status"] == "missing" and r["value"] is None


def test_vol_regime_is_in_macro_bloc_and_nudges_down():
    """vol_regime sits in the de-correlated macro bloc, so a risk-off vol read can only push the
    ONE net macro vote toward bear (caution) — it never adds an independent vote that sizes alone."""
    assert "vol_regime" in lenses._MACRO_BLOC
    # macro bloc otherwise neutral; vol_regime stress flips the net macro vote bearish
    fake = {"rows": [
        {"lens": "macro_risk", "direction": "neutral", "value": {}},
        {"lens": "vol_regime", "direction": "bear", "value": _vol_mm()},
        {"lens": "conviction", "direction": "bull", "value": {"band": "high"}},
    ]}
    s = lenses.synthesize(fake)
    assert s["bloc_macro"] == "bear"
