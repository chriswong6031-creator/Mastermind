"""Decision-engine accuracy battery — the surrogate for 'backtest a variety of tickers' when no
live data is checked out. Two layers:

  PART A — targeted unit tests for each correctness fix (PEG/cheap-pctile/13F/sector-rs-crash/
           divergence-on-blocked/commodity-fail-safe/research-gate/derive-check).
  PART B — ARCHETYPAL end-to-end scenarios: a synthetic but schema-faithful stockdata+regime profile
           per archetype is run through the REAL lens stack (lenses.full) and the gate verdict is
           asserted. This is the regression net for false positives (a bad name that must NOT pass)
           and false negatives (a good name that must pass).

All offline — synthetic dicts + monkeypatched price/commodity seams. No network, no submodule.
"""
from __future__ import annotations

from datetime import date, timedelta
from unittest import mock

import bot  # noqa: F401  -> vendor/macro onto sys.path

from portfolio import lenses
import portfolio.lenses as _lenses_mod


# ===========================================================================
# PART A — targeted unit tests per fix
# ===========================================================================

def test_negative_forward_pe_is_not_cheap_for_growth():
    """A loss-making name (negative forward P/E) must NOT read valuation-bull via a negative PEG."""
    d, peg = lenses._valuation_dir(value_z=-0.5, cheap=20, fwd=-15.0, rev_cagr=30.0, eps_cagr=None)
    assert peg is None, "negative forward P/E must yield no PEG (not a negative one)"
    assert d == "bear", "expensive + loss-making must read bear, not bull"


def test_positive_peg_cheap_for_growth_still_bull():
    """A genuinely cheap-for-growth name (low POSITIVE PEG) still reads bull."""
    d, peg = lenses._valuation_dir(value_z=-0.4, cheap=40, fwd=18.0, rev_cagr=40.0, eps_cagr=None)
    assert peg is not None and 0 < peg < 0.8
    assert d == "bull"


def test_cheap_zero_percentile_reads_expensive_not_neutral():
    """`cheap`=0 is a real (very expensive) percentile, not missing — must not be masked to mid."""
    d, _ = lenses._valuation_dir(value_z=None, cheap=0, fwd=None, rev_cagr=None, eps_cagr=None)
    assert d == "bear", "0th-percentile-cheap (i.e. expensive) must read bear, not neutral"
    # and a genuinely cheap percentile still reads bull
    d2, _ = lenses._valuation_dir(value_z=None, cheap=80, fwd=None, rev_cagr=None, eps_cagr=None)
    assert d2 == "bull"


def test_13f_min_sample_floor():
    """Fewer than 3 tracked decisions is too thin to fire a direction."""
    assert lenses._flows_13f_dir(2, 0) == "neutral"     # 2-0 total 2 -> thin
    assert lenses._flows_13f_dir(0, 2) == "neutral"     # 0-2 total 2 -> thin
    assert lenses._flows_13f_dir(5, 1) == "bull"        # total 6, margin 4
    assert lenses._flows_13f_dir(1, 4) == "bear"        # total 5, margin 3
    assert lenses._flows_13f_dir(2, 1) == "neutral"     # total 3 but margin 1 -> no direction
    assert lenses._flows_13f_dir(None, None) is None    # no coverage


def _patch_load(regime=None, stock=None, ticker=None):
    """A _load dispatcher returning the given regime / per-ticker stock, None for everything else."""
    def _ld(rel):
        if rel == "data/regime/latest.json":
            return regime
        if ticker and rel == f"site/stockdata/{ticker}.json":
            return stock
        return None
    return _ld


def test_sector_rs_no_crash_on_null_pctile():
    """A sector ETF present in the RS table but with pctile_252d=None must not raise (it used to
    TypeError in the f-string, which conviction.build silently swallowed as a dropped name)."""
    regime = {"sector_rs": [{"ticker": "XLK", "rank": 3, "pctile_252d": None,
                             "above_200d_trend": True, "mom_60d_pct": 5}]}
    with mock.patch.object(_lenses_mod, "_load", side_effect=_patch_load(regime=regime)):
        row = lenses._sector_rs_row({"ticker": "AAA", "sector": "Technology"})
    assert row["status"] == "validated"
    assert "pctile N/A" in row["note"]


def test_divergence_high_confluence_buy_suppressed_when_blocked():
    """A hard-vetoed name must not be labelled a 'full-size candidate' divergence."""
    rows = [
        {"lens": "conviction", "value": {"band": "strong"}, "direction": "bull"},
        {"lens": "valuation", "value": {}, "direction": "bull"},
        {"lens": "flows_13f", "value": {}, "direction": "bull"},
        {"lens": "extension", "value": {"parabolic": True}, "direction": "bear"},
    ]
    patterns = [d["pattern"] for d in lenses._divergences(rows)]
    assert "high_confluence_buy" not in patterns, "blocked name must not show a buy divergence"


def test_divergence_high_confluence_buy_present_when_clean():
    rows = [
        {"lens": "conviction", "value": {"band": "strong"}, "direction": "bull"},
        {"lens": "valuation", "value": {}, "direction": "bull"},
        {"lens": "flows_13f", "value": {}, "direction": "bull"},
        {"lens": "extension", "value": {"parabolic": False}, "direction": "neutral"},
    ]
    patterns = [d["pattern"] for d in lenses._divergences(rows)]
    assert "high_confluence_buy" in patterns


def test_commodity_miner_fail_safe_when_price_store_cold():
    """When the commodity's price history is unavailable AND the miner is below its own 200dma,
    the leadership lens must fail toward bear rather than silently passing (NEM-trap regression)."""
    regime = {"sector_rs": []}   # no GC=F, no XLB
    with mock.patch.object(_lenses_mod, "_load", side_effect=_patch_load(regime=regime)), \
         mock.patch.object(_lenses_mod, "_commodity_regime", return_value=None):
        row = lenses._sector_rs_row({"ticker": "NEM", "sector": "Materials",
                                     "tech": {"above200": False}})
    assert row["direction"] == "bear"
    assert row["value"].get("fallback") == "own_200dma"


def test_commodity_miner_no_data_and_healthy_own_trend_stays_neutral():
    """If the commodity data is cold but the miner is above its own 200dma, don't over-block —
    fall through to 'missing' (direction None), not a forced bear."""
    regime = {"sector_rs": []}
    with mock.patch.object(_lenses_mod, "_load", side_effect=_patch_load(regime=regime)), \
         mock.patch.object(_lenses_mod, "_commodity_regime", return_value=None):
        row = lenses._sector_rs_row({"ticker": "NEM", "sector": "Materials",
                                     "tech": {"above200": True}})
    assert row["direction"] is None


def test_research_gate_engine_mode_not_recommend_blocks():
    """An engine-mode 'rich' paper (recommend=False) must NOT confirm even with a high combined."""
    from brain import research_paper as rp
    paper = {"research_score": 80, "viability": "rich", "mode": "engine", "recommend": False}
    g = rp.score_breakdown(0.5, paper)
    assert g["confirmed"] is False
    assert "does not recommend" in g["reason"]


def test_research_gate_recommend_true_confirms():
    from brain import research_paper as rp
    paper = {"research_score": 80, "viability": "compelling", "mode": "engine", "recommend": True}
    g = rp.score_breakdown(0.5, paper)
    assert g["confirmed"] is True


def test_generate_redigest_parse_failure_downgrades_to_engine():
    """If the armed re-digest cannot be parsed, the paper must be 'engine' mode (not 'llm') so it is
    graded by the conservative deterministic recommend, not a defaulted recommend=True."""
    from brain import research_paper as rp
    rows = [{"lens": "valuation", "value": {}, "direction": "neutral", "status": "context"}]
    long_report = "## Thesis\n" + ("lorem ipsum dolor sit amet " * 30)
    with mock.patch.object(rp.cli_bridge, "research_sync",
                           return_value={"ok": True, "text": long_report, "model": "test"}), \
         mock.patch.object(rp.cli_bridge, "reason_sync", return_value={"text": "not-json"}), \
         mock.patch.object(rp, "_parse_verdict", return_value=None):
        paper = rp.generate("ZZZ", asof="2026-06-20", confluence=0.2, rows=rows,
                            vetoes=[], price=10.0, regime={"quad": "Q1"}, armed=True)
    assert paper["verdict_parsed"] is False
    assert paper["mode"] == "engine", "an ungradeable re-digest must not be marked llm-approved"


def test_derive_check_non_directional_is_kind_none():
    from brain.decision import derive_check
    assert derive_check("X", "watch", 21)["kind"] == "none"
    assert derive_check("X", "hold", 21)["kind"] == "none"
    assert derive_check("X", "add", 21)["op"] == "<"
    assert derive_check("X", "avoid", 21)["op"] == ">"


def test_decisiondoc_watch_falsifier_is_non_directional():
    from brain.decision import DecisionDoc
    doc = DecisionDoc(id="d1", subject="X", lean="watch", conviction="low", prob_correct=0.55,
                      horizon_d=21, state_asof="2026-06-20", thesis="t").finalize()
    assert doc.falsifier["check"]["kind"] == "none"
    assert "non-directional" in doc.falsifier["text"]


def test_scorer_skips_non_directional_thesis():
    """A 'watch' thesis must not be graded as a bet (it used to be scored wrong whenever the name
    rallied, and would KeyError on the missing op)."""
    from brain import scorer
    from brain.decision import DecisionDoc
    past = (date.today() - timedelta(days=40)).isoformat()
    watch = DecisionDoc(id="w1", subject="WW", lean="watch", conviction="low", prob_correct=0.55,
                        horizon_d=10, state_asof=past, thesis="t").finalize().to_json()
    bull = DecisionDoc(id="b1", subject="BB", lean="add", conviction="medium", prob_correct=0.6,
                       horizon_d=10, state_asof=past, thesis="t").finalize().to_json()
    with mock.patch.object(scorer, "all_theses", return_value=[watch, bull]):
        tr = scorer.track_record(asof=date.today(), realized={"w1": 0.2, "b1": -0.1})
    assert tr["n"] == 1, "only the directional (add) thesis should be graded; the watch is skipped"


# ===========================================================================
# PART B — archetypal end-to-end battery (real lens stack via synthetic data)
# ===========================================================================

def _mk_stock(ticker="AVGO", sector="Technology", *, value_z=0.5, cheap=72, forward_pe=24.0,
              rev_cagr=30.0, eps_cagr=24.0, quality_z=0.6, accounting=None, altman="safe",
              altman_approx=False,
              piotroski=7, mfe=22.0, dd_avg=-10.0, dd_tail=-15.0, ext_grade="ok", parabolic=False,
              pct_vs_200dma=14.0, above50=True, above200=True, macd=True, rsi=60.0,
              off_52w_high=-6.0, pct_vs_50dma=3.0, price=200.0, nb=5, ns=1, band="strong",
              conv_score=80, size_pct=5, cycle_blocked=False, macro_regime="tailwind", baskets=None):
    d = {
        "ticker": ticker, "sector": sector,
        "valuation": {"value_z": value_z, "trailing_pe": {"cheap": cheap}, "forward_pe": forward_pe},
        "financials": {"multiyear": {"rev_cagr": rev_cagr, "eps_cagr": eps_cagr,
                                     "altman": {"zone": altman, "approx": altman_approx},
                                     "piotroski": {"score": piotroski}}},
        "conviction": {"axes": {"quality": {"z": quality_z, "flags": {"accounting": accounting}}},
                       "ext": {"grade": ext_grade, "parabolic": parabolic},
                       "band": band, "score": conv_score, "size": {"pct": size_pct},
                       "verdict": "add", "cycle_blocked": cycle_blocked},
        "anticipation": {"horizons": {"medium": {"mfe_med": mfe, "dd_avg": dd_avg,
                                                  "dd_tail": dd_tail, "p_up": 0.6, "thin": False}}},
        "tech": {"above50": above50, "above200": above200, "macd_pos": macd, "rsi14": rsi,
                 "off_52w_high_pct": off_52w_high, "pct_vs_50dma": pct_vs_50dma,
                 "pct_vs_200dma": pct_vs_200dma, "price": price},
        "smart_money": {"n_buying": nb, "n_selling": ns, "vip": []},
        "macro_sensitivity": {"tier": 1, "duration": "short", "regime": macro_regime},
    }
    if baskets:
        d["baskets_membership"] = baskets
    return d


def _mk_regime(*, sector_etf="XLK", pctile=85.0, above200=True, mom60=10.0, rank=1,
               macro_risk=0.2, cuts=2, cross_asset="broad", quad="Q1"):
    return {
        "date": "2026-06-20", "quad": quad, "quad_name": "Goldilocks", "liquidity_overlay": "expanding",
        "macro_risk": {"score": macro_risk}, "fed_path": {"implied_cuts_12m": cuts, "headline": "cuts"},
        "cross_asset": {"verdict": cross_asset, "absorption_ratio": 0.3},
        "sector_rs": [{"ticker": sector_etf, "rank": rank, "pctile_252d": pctile,
                       "above_200d_trend": above200, "mom_60d_pct": mom60}],
    }


def _run(stock, regime, ticker, *, recent_return=None, commodity=None):
    """Run the REAL lens stack against synthetic data; return lenses.full(ticker)."""
    patches = [mock.patch.object(_lenses_mod, "_load",
                                 side_effect=_patch_load(regime=regime, stock=stock, ticker=ticker))]
    if recent_return is not None:
        patches.append(mock.patch.object(_lenses_mod, "_recent_return",
                                         side_effect=lambda t, n: recent_return))
    if commodity is not None:
        patches.append(mock.patch.object(_lenses_mod, "_commodity_regime", return_value=commodity))
    import contextlib
    with contextlib.ExitStack() as es:
        for p in patches:
            es.enter_context(p)
        return lenses.full(ticker, "name")


def _dir(full, lens):
    return next((r["direction"] for r in full["rows"] if r["lens"] == lens), "MISSING")


def test_archetype_clean_leader_passes():
    full = _run(_mk_stock(ticker="AVGO"), _mk_regime(), "AVGO")
    s = full["synthesis"]
    assert s["size_authority"] == "up", f"a clean leader must pass; got {s}"
    assert not s["vetoes"]
    assert "high_confluence_buy" in [d["pattern"] for d in s["divergences"]]


def test_archetype_parabolic_blocked():
    full = _run(_mk_stock(ticker="NVDA", parabolic=True, pct_vs_200dma=55), _mk_regime(), "NVDA")
    s = full["synthesis"]
    assert "parabolic" in s["vetoes"]
    assert s["size_authority"] == "blocked"


def test_archetype_altman_distress_blocked():
    # complete-data distress in a sector where Altman Z is valid (Tech) → the hard veto fires.
    full = _run(_mk_stock(ticker="XXX", sector="Technology", altman="distress"), _mk_regime(), "XXX")
    s = full["synthesis"]
    assert "altman_distress" in s["vetoes"]
    assert s["size_authority"] == "blocked"


def test_archetype_altman_distress_sector_demoted():
    # Altman Z is structurally invalid for high-leverage non-manufacturers — a distress read on a
    # utility/financial/REIT is CONTEXT, not a hard veto, so it must NOT size the name to 0.
    for sector in ("Utilities", "Financials", "Real Estate"):
        full = _run(_mk_stock(ticker="UTL", sector=sector, altman="distress"), _mk_regime(), "UTL")
        s = full["synthesis"]
        assert "altman_distress" not in s["vetoes"], f"{sector} distress must be context, not a veto"
        assert s["size_authority"] != "blocked", f"{sector} must not be hard-blocked by Altman"
        solv = next(r for r in full["rows"] if r["lens"] == "solvency")
        assert solv["value"]["altman_context"] == "sector-invalid"


def test_archetype_altman_distress_approx_demoted():
    # a distress score computed WITHOUT the X4 leverage leg (approx=True: liabilities missing and
    # un-reconstructable) is too incomplete to hard-block on — demote to context.
    full = _run(_mk_stock(ticker="APX", sector="Technology", altman="distress", altman_approx=True),
                _mk_regime(), "APX")
    s = full["synthesis"]
    assert "altman_distress" not in s["vetoes"]
    assert s["size_authority"] != "blocked"
    solv = next(r for r in full["rows"] if r["lens"] == "solvency")
    assert solv["value"]["altman_context"] == "approx-data"


def test_archetype_gold_miner_in_gold_bear_blocked():
    """NEM (maps to GC=F, absent from sector_rs) with gold in a bear regime must fail leadership."""
    bear = {"above_200d_trend": False, "mom_60d_pct": -15.0, "downtrend": True}
    full = _run(_mk_stock(ticker="NEM", sector="Materials"),
                _mk_regime(sector_etf="XLK"), "NEM", commodity=bear)
    s = full["synthesis"]
    assert _dir(full, "sector_rs") == "bear"
    assert s["sector_lagging"] is True and s["leadership_ok"] is False
    assert s["size_authority"] != "up"


def test_archetype_gold_miner_in_gold_bull_allowed():
    bull = {"above_200d_trend": True, "mom_60d_pct": 14.0, "downtrend": False}
    full = _run(_mk_stock(ticker="NEM", sector="Materials"),
                _mk_regime(), "NEM", commodity=bull)
    assert _dir(full, "sector_rs") == "bull"
    assert full["synthesis"]["leadership_ok"] is True


def test_archetype_falling_knife_blocked():
    full = _run(_mk_stock(ticker="LPG"), _mk_regime(), "LPG", recent_return=-0.12)
    s = full["synthesis"]
    assert s["price_falling_fast"] is True
    assert s["size_authority"] != "up"


def test_archetype_value_trap_lagging_sector_blocked():
    """Cheap, but its sector is a confirmed laggard and there's no leading-theme escape."""
    full = _run(_mk_stock(ticker="RF", sector="Financials", value_z=0.6, cheap=80),
                _mk_regime(sector_etf="XLF", pctile=15, above200=False, mom60=-15, rank=11), "RF")
    s = full["synthesis"]
    assert _dir(full, "sector_rs") == "bear"
    assert s["leadership_ok"] is False
    assert s["size_authority"] != "up"


def test_archetype_weak_asymmetry_blocked():
    full = _run(_mk_stock(ticker="SYM", mfe=8.0, dd_avg=-10.0), _mk_regime(), "SYM")
    s = full["synthesis"]
    assert s["weak_asymmetry"] is True
    assert s["size_authority"] != "up"


def test_archetype_healthy_pullback_passes():
    """Below the 50dma but above a rising 200dma, near the high, no freefall -> dip-buy, not a trap."""
    full = _run(_mk_stock(ticker="AVGO", above50=False, macd=False, rsi=48, off_52w_high=-9,
                          pct_vs_50dma=-2), _mk_regime(), "AVGO")
    s = full["synthesis"]
    assert _dir(full, "trend") != "bear", "a healthy pullback is not a downtrend"
    assert s["price_downtrend"] is False
    assert s["size_authority"] == "up"


def test_archetype_negative_pe_valuation_not_bull():
    """The headline false-positive: a loss-maker (negative fwd P/E) must not read valuation bull."""
    full = _run(_mk_stock(ticker="LOSS", value_z=-0.5, cheap=20, forward_pe=-15.0, rev_cagr=30.0),
                _mk_regime(), "LOSS")
    assert _dir(full, "valuation") == "bear"


def test_archetype_thin_13f_is_neutral():
    full = _run(_mk_stock(ticker="THIN", nb=2, ns=0), _mk_regime(), "THIN")
    assert _dir(full, "flows_13f") == "neutral"
