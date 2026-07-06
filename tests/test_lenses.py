"""The multi-sided decision matrix — every lens read from live data, gated by confluence/divergence."""
import asyncio
import json

import pytest

import bot  # noqa: F401

from portfolio import lenses
from brain import bot_mcp

# The three *_live / *_nvda tests below read the REAL per-name stockdata (site/stockdata/NVDA.json)
# out of the vendored render. A fresh CI checkout / data-outage worktree has no such file, so NVDA
# takes the fail-closed degenerate branch (only context lenses fire) and these live-data assertions
# can't hold. Skip them when the substrate is absent — they still exercise real data on a real render
# — rather than either failing spuriously OR (worse) locking in the pre-fix buggy 'up' authority that
# a data-less NVDA used to return. The fail-closed behaviour itself is covered by the dedicated
# test_absent_stockdata_* tests, which inject their own matrices and do not need vendor data.
_NVDA_HAS_STOCKDATA = lenses._load("site/stockdata/NVDA.json") is not None
_needs_nvda = pytest.mark.skipif(not _NVDA_HAS_STOCKDATA,
                                 reason="no vendored site/stockdata/NVDA.json in this checkout")


@_needs_nvda
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


@_needs_nvda
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


# ---------------- FAIL-CLOSED data-coverage gate (the 2026-07-01 fail-open incident) ----------------
def test_absent_stockdata_is_data_degraded_and_never_sizes_up(monkeypatch):
    """The core 07-01 bug: a name with NO stockdata used to yield the alt-data-only degenerate matrix
    (n_scored=1, confluence=1.0, size_authority='up') — a feed outage minted a full-conviction buy.
    With the fail-closed gate that name must read size_authority='insufficient_data' (NEW value, not
    'up') and data_degraded=True. This ENFORCES doctrine ('one dim alone is forbidden') — a lone
    alt-data context lens can never earn a size-up on its own."""
    # only the alt-data feed resolves; site/stockdata/{t}.json is absent (returns None) — exactly the
    # outage shape. by_ticker gives EFX a real buy-side alt-data row so it WOULD have voted bull.
    bt = {"tickers": {"EFX": {"convergence_score": 2, "channels": ["gov_contract"], "trump_linked": True,
                              "trump_side": "buy", "gov_contract_usd_30d": 7e7}}}
    monkeypatch.setattr(lenses, "_load", lambda rel: bt if "altdata/by_ticker" in rel else None)
    f = lenses.full("EFX", "name")
    s = f["synthesis"]
    assert s["size_authority"] == "insufficient_data"     # NOT 'up' — the fix
    assert s["data_degraded"] is True and s["stockdata_present"] is False
    # existing authority values are untouched (downstream string checks still work)
    assert s["size_authority"] not in ("up", "down", "hold", "blocked")
    # the truth is surfaced in explicit fields so artifacts/logs show WHY (confluence reported as-is)
    assert "n_scored" in s and "data_degraded" in s


def test_thin_evidence_under_two_lenses_is_degraded():
    """Fewer than 2 real directional votes (n_scored < 2) is degraded even if stockdata was 'present'
    — a single lens can't authorize a buy. A single bull vote must not read 'up'."""
    fake = {"rows": [
        {"lens": "conviction", "direction": "bull", "value": {"band": "high", "stockdata_present": True}},
    ]}
    s = lenses.synthesize(fake)
    assert s["n_scored"] == 1 and s["data_degraded"] is True
    assert s["size_authority"] == "insufficient_data"


def test_full_coverage_is_not_degraded():
    """Regression: a matrix with full stockdata (the conviction sentinel present, >=2 real votes) is
    NOT degraded and the gate behaves exactly as before — degraded flag off, authority is one of the
    original four, confluence math unchanged. (Built from an explicit matrix so the test doesn't
    depend on live vendor stockdata being present in the checkout.)"""
    fake = {"rows": [
        {"lens": "trend", "direction": "bull", "value": {"confirmed_uptrend": True}},
        {"lens": "sector_rs", "direction": "bull", "value": {}},
        {"lens": "valuation", "direction": "bull", "value": {}},
        {"lens": "conviction", "direction": "bull",
         "value": {"band": "high", "stockdata_present": True}},   # sentinel present => not the outage branch
    ]}
    s = lenses.synthesize(fake)
    assert s["data_degraded"] is False and s["stockdata_present"] is True
    assert s["size_authority"] in ("up", "down", "hold", "blocked")
    assert s["n_scored"] >= 2


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


@_needs_nvda
def test_nvda_growth_leader_passes_gate():
    # regression: NVDA must no longer be a tight-factor false-reject. With PEG-aware valuation and
    # the 13F gate, the 'distribution' divergence does not fire and the gate sizes it.
    f = lenses.full("NVDA")
    rows = {r["lens"]: r for r in f["rows"]}
    assert rows["valuation"]["direction"] != "bear"
    assert "distribution" not in [d["pattern"] for d in f["synthesis"]["divergences"]]
    # Intent-only (live-data): NVDA must not be FALSELY rejected — 'blocked'/'insufficient_data'
    # would resurrect the old false-reject; 'up' vs hysteresis-'hold' tracks the live tape and
    # flapped with the 2026-07-02 R2-synced vintage, so we assert the failure modes are absent.
    assert f["synthesis"]["size_authority"] in ("up", "hold") and not f["synthesis"]["vetoes"]


def test_flow_lens_direction_from_reliable_doi_only(monkeypatch):
    """Options-flow DIRECTION comes ONLY from the reliable day-over-day ΔOI positioning — the SOFT
    signed flow (signed_pc / net_premium sign) carries no directional weight. status is always
    'context' (direction is soft-sourced even when positioning is reliable)."""
    mm = {"names": {
        "PUTHVY": {"net_doi": 0, "positioning_lean": None, "signed_pc": 2.9, "net_premium_mn": 400, "reliable": True},
        "CALLBLD": {"net_doi": 1500000, "positioning_lean": "net new CALL positioning", "reliable": True},
        "PUTBLD": {"net_doi": 1200000, "positioning_lean": "net new PUT positioning (defensive)", "reliable": True},
        "NOPOS": {"net_doi": None, "net_premium_mn": 30},
    }}
    monkeypatch.setattr(lenses, "_load", lambda rel: mm if "flow/mastermind" in rel else None)
    assert lenses._flow_row("PUTHVY")["direction"] == "neutral"   # soft put-heavy ignored (ΔOI flat)
    assert lenses._flow_row("CALLBLD")["direction"] == "bull"
    assert lenses._flow_row("PUTBLD")["direction"] == "bear"
    assert lenses._flow_row("NOPOS")["direction"] == "neutral"    # magnitude context only, no direction
    assert all(lenses._flow_row(t)["status"] == "context" for t in ("PUTHVY", "CALLBLD", "PUTBLD", "NOPOS"))
    assert lenses._flow_row("UNKNOWN") is None


def test_flow_lens_is_independent_and_never_sizes_alone():
    """options_flow is INDEPENDENT evidence (not in the fund/macro blocs) and a lone flow-bull
    among neutrals must NOT flip size_authority to 'up' (a single context vote « the 0.30 bar)."""
    assert "options_flow" not in lenses._FUND_BLOC and "options_flow" not in lenses._MACRO_BLOC
    fake = {"rows": [
        {"lens": "options_flow", "direction": "bull", "value": {}, "status": "context"},
        {"lens": "valuation", "direction": "neutral", "value": {}},
        {"lens": "quality", "direction": "neutral", "value": {}},
        {"lens": "conviction", "direction": "neutral", "value": {"band": "neutral"}},
    ]}
    assert lenses.synthesize(fake)["size_authority"] != "up"


# ---------------- vol-regime context lens (subtract-only gross caution) ----------------
def _vol_mm(regime="backwardation-stress", **extra):
    return {"schema": "vol_regime.context.v1", "regime": regime, "kill_switch": regime == "backwardation-stress",
            "vol_target_scalar": 0.7, "scored_active": False, "scored_score": None,
            "ts_slope_state": "backwardation", "fragility_confluence": 3, **extra}


def test_vol_regime_lens_present_and_subtract_only(monkeypatch):
    """The vol-regime lens votes BEAR in a risk-off state and NEUTRAL when calm.
    It can never vote BULL — subtract-only gross caution (see structural invariant test below).
    It rides in _macro_rows so every name/theme matrix with macro context carries it.

    docket F7 / R7 enforcement (ASYMMETRIC, Fable ruling 2026-07-06):
      • scored_active=False KEEPS tightening (bear stays bear) — unvalidated caution is safe.
      • scored_active=False SUPPRESSES loosening (bull → neutral), but since the lens is
        structurally subtract-only (never bull), this only matters as an invariant.
    """
    # scored_active=True, risk-off => bear (validated data affects sizing, tightening allowed)
    monkeypatch.setattr(lenses, "_load",
                        lambda rel: {**_vol_mm(), "scored_active": True} if "vol/mastermind" in rel else None)
    monkeypatch.delenv("MASTERMIND_VOL_REGIME_SCORED_GATE", raising=False)
    r = lenses._vol_regime_row()
    assert r["lens"] == "vol_regime" and r["status"] == "context" and r["direction"] == "bear"
    assert r["value"]["regime"] == "backwardation-stress" and r["value"]["vol_target_scalar"] == 0.7
    assert "vol_regime" in {row["lens"] for row in lenses._macro_rows()}   # wired into the macro rows

    # scored_active=False, risk-off => BEAR (Fable ruling: tightening kept — asymmetric enforcement)
    monkeypatch.setattr(lenses, "_load",
                        lambda rel: _vol_mm() if "vol/mastermind" in rel else None)  # _vol_mm has scored_active=False
    r_unscored = lenses._vol_regime_row()
    assert r_unscored["direction"] == "bear", (
        "scored_active=False must KEEP direction='bear' for risk-off regimes. "
        "Tightening (caution) is always allowed — asymmetric enforcement (Fable ruling 2026-07-06). "
        "Only loosening (bull→neutral) is suppressed. "
        "Got: " + repr(r_unscored["direction"])
    )
    assert r_unscored["value"]["tier_enforced"] is True

    # calm (scored_active=True) => neutral (never bull — subtract-only)
    monkeypatch.setattr(lenses, "_load",
                        lambda rel: {**_vol_mm(regime="normalizing", kill_switch=False), "scored_active": True}
                        if "vol/mastermind" in rel else None)
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


def test_vol_regime_subtract_only_structural_invariant(monkeypatch):
    """Pinned structural invariant: vol_regime is SUBTRACT-ONLY — can never produce 'bull'.

    Fable ruling (2026-07-06): enforcement is ASYMMETRIC — tightening (bear) always passes,
    loosening (bull) from unvalidated data is suppressed.  The current implementation computes
    raw_direction as 'bear' if risk_off else 'neutral', making 'bull' structurally unreachable.
    This test pins that invariant so any future code change that adds a bull path is caught
    immediately.  Covers: all known regime strings × scored_active {True, False} × gate {on, off}.
    """
    all_regimes = ["warning", "backwardation-stress", "normalizing", "normal", "calm",
                   "elevated", "fragile", "unknown", "", None]

    for regime in all_regimes:
        for gate_env in ("1", "0"):
            for sa in (True, False):
                fake_vol = {"regime": regime, "scored_active": sa,
                            "kill_switch": False, "vol_target_scalar": 1.0}

                monkeypatch.setattr(lenses, "_load",
                                    lambda rel, _fv=fake_vol: _fv if "vol" in rel else None)
                monkeypatch.setenv("MASTERMIND_VOL_REGIME_SCORED_GATE", gate_env)

                row = lenses._vol_regime_row()
                assert row["direction"] != "bull", (
                    f"vol_regime produced 'bull' — SUBTRACT-ONLY structural invariant violated: "
                    f"regime={regime!r}, scored_active={sa}, gate={gate_env}. "
                    "This lens must never loosen gross (no bull vote)."
                )


def test_vol_regime_unscored_bear_kept_asymmetric(monkeypatch):
    """Fable ruling 2026-07-06 — asymmetric enforcement:
    scored_active=False with a risk-off regime KEEPS direction='bear' (tightening is safe).
    The earlier implementation forced bear→neutral on scored_active=False; that was the
    blocker being reverted here.
    """
    fake_vol = {
        "regime": "warning",       # risk_off=True
        "kill_switch": False,
        "vol_target_scalar": 0.8,
        "scored_active": False,    # display-only
    }
    monkeypatch.setattr(lenses, "_load",
                        lambda rel: fake_vol if "vol" in rel else None)
    monkeypatch.delenv("MASTERMIND_VOL_REGIME_SCORED_GATE", raising=False)

    row = lenses._vol_regime_row()
    assert row["direction"] == "bear", (
        "scored_active=False + risk-off must KEEP direction='bear' (asymmetric enforcement). "
        "Tightening caution is always safe regardless of validation tier. "
        f"Got: {row['direction']!r}"
    )
    assert row["value"]["tier_enforced"] is True
    assert row["value"]["scored_active"] is False
